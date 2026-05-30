"""자동화 잡 모음 — 데일리 카드 생성 / 헤드리스 발행.

server.py 의 기존 함수(generate_cards, _compose_source_text, dailysync_conn,
save_generated_session) 를 그대로 재사용한다. 순환 임포트 피하려고
잡 함수 안에서 lazy import.

스케줄:
  - job_generate_daily       매일 07:30 KST — 뉴스 1 + 툴팁 1 자동 생성 → 세션 저장
  - job_publish_due          매 15분 — scheduled_publish_at 도래한 세션 헤드리스 발행

세션 메타에 추가되는 필드:
  - scheduled_publish_at : ISO datetime (UTC) — 자동 발행 예약 시각
  - published_at         : ISO datetime (UTC) — 실제 발행 시각
  - ig_media_id          : str — IG Graph API 반환 media id
  - publish_status       : "scheduled" | "publishing" | "published" | "failed"
  - publish_error        : 실패 시 메시지
"""
import hashlib
import json
import logging
import os
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).parent
TOPICS_PATH = ROOT / "data" / "tooltip_topics.yaml"
SESSIONS_DIR = ROOT / "sessions"

# 게시 기본 시각 (KST). .env 에서 덮어쓸 수 있음.
NEWS_POST_HOUR = int(os.getenv("INSTA_NEWS_POST_HOUR", "12"))
TOOLTIP_POST_HOUR = int(os.getenv("INSTA_TOOLTIP_POST_HOUR", "19"))

# 툴팁 주제 회피 기간 (일)
TOOLTIP_AVOID_DAYS = int(os.getenv("TOOLTIP_AVOID_DAYS", "30"))

# 자동 게시 토글 — false 면 자동 생성만 하고 게시는 사람이 클릭해야 함
AUTO_PUBLISH = os.getenv("AUTO_PUBLISH", "true").lower() == "true"


# ============================================================
# 툴팁 주제 풀 로드 + 로테이션
# ============================================================
def _load_tooltip_pool() -> list[dict]:
    """tooltip_topics.yaml → 평면화. 각 원소: {tool, label, subtopic, key}."""
    if not TOPICS_PATH.exists():
        logger.warning(f"tooltip_topics.yaml 없음: {TOPICS_PATH}")
        return []
    with open(TOPICS_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    pool = []
    for entry in raw:
        tool = entry.get("tool")
        label = entry.get("label") or tool
        for sub in entry.get("subtopics") or []:
            pool.append({
                "tool": tool,
                "label": label,
                "subtopic": sub,
                "key": f"{tool}::{hashlib.md5(sub.encode('utf-8')).hexdigest()[:8]}",
            })
    return pool


def _recent_used_tooltip_keys(days: int = TOOLTIP_AVOID_DAYS) -> set[str]:
    """최근 N일 안에 사용된 (tool, subtopic) 키 집합 — 세션 메타에서 추출."""
    cutoff = time.time() - days * 86400
    used = set()
    for f in SESSIONS_DIR.glob("*.json"):
        if f.stat().st_mtime < cutoff:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            meta = data.get("meta") or {}
            if meta.get("kind") == "tool_tip" and meta.get("topic_key"):
                used.add(meta["topic_key"])
        except Exception:
            continue
    return used


def pick_today_tooltip() -> dict | None:
    """오늘(KST) 사용할 툴팁 주제 1개 deterministic 선택."""
    pool = _load_tooltip_pool()
    if not pool:
        return None
    used = _recent_used_tooltip_keys()
    fresh = [p for p in pool if p["key"] not in used] or pool
    today_ord = datetime.now(KST).date().toordinal()
    return fresh[today_ord % len(fresh)]


# ============================================================
# 톱 클러스터 선정 (데일리싱크 DB)
# ============================================================
def _used_cluster_ids() -> set[int]:
    """이미 카드뉴스로 변환된 cluster_id 집합 (세션 메타에서 추출)."""
    used = set()
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            meta = data.get("meta") or {}
            cid = meta.get("cluster_id")
            if isinstance(cid, int):
                used.add(cid)
        except Exception:
            continue
    return used


def pick_top_cluster() -> int | None:
    """오늘 카드뉴스로 만들 톱 클러스터 ID 반환.

    정렬: importance DESC, saved 우선, recent.
    이미 사용된 클러스터는 제외. 데일리싱크 DB 없으면 None.
    """
    from server import dailysync_conn  # lazy import (순환 회피)
    try:
        con = dailysync_conn()
    except Exception as e:
        logger.warning(f"데일리싱크 DB 접근 실패: {e}")
        return None

    used = _used_cluster_ids()
    # 최근 2일치 + summary 있는 것 + hidden 제외
    today_kst = datetime.now(KST).date()
    since_kst = today_kst - timedelta(days=1)
    try:
        rows = con.execute(
            "SELECT id, importance, saved_at, first_shown_date, created_at "
            "FROM clusters "
            "WHERE summary_ko IS NOT NULL AND hidden_at IS NULL "
            "  AND (first_shown_date >= ? OR first_shown_date IS NULL) "
            "ORDER BY importance DESC, saved_at IS NOT NULL DESC, id DESC "
            "LIMIT 30",
            (since_kst.isoformat(),),
        ).fetchall()
    finally:
        con.close()

    for r in rows:
        if r["id"] not in used:
            return int(r["id"])
    return None


# ============================================================
# 잡 1) 매일 07:30 — 뉴스 + 툴팁 자동 생성
# ============================================================
def _schedule_publish_dt(content_kind: str) -> datetime:
    """오늘 KST 기준 발행 예약 시각을 UTC 로 반환."""
    today_kst = datetime.now(KST).date()
    hour = NEWS_POST_HOUR if content_kind == "daily_news" else TOOLTIP_POST_HOUR
    target_kst = datetime.combine(today_kst, datetime.min.time(),
                                  tzinfo=KST).replace(hour=hour)
    # 이미 지난 시각이면 다음날
    if target_kst < datetime.now(KST):
        target_kst += timedelta(days=1)
    return target_kst.astimezone(timezone.utc).replace(tzinfo=None)


def _attach_publish_schedule(session_id: str, content_kind: str,
                             topic_key: str | None = None) -> None:
    """세션 JSON 파일에 자동 발행 예약 메타 주입."""
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.get("meta") or {}
    meta["scheduled_publish_at"] = _schedule_publish_dt(content_kind).isoformat()
    meta["publish_status"] = "scheduled" if AUTO_PUBLISH else "manual"
    if topic_key:
        meta["topic_key"] = topic_key
    data["meta"] = meta
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def job_generate_daily() -> dict:
    """매일 07:30 KST — 톱 뉴스 1 + 오늘의 툴팁 1 자동 생성."""
    from server import (
        generate_cards, _compose_source_text, save_generated_session,
        DEFAULT_BRAND, DEFAULT_TONE,
    )

    stats = {"news_created": 0, "tooltip_created": 0,
             "news_skipped": 0, "tooltip_skipped": 0,
             "errors": []}

    # ---- 1. 뉴스 카드 ----
    try:
        cid = pick_top_cluster()
        if cid is None:
            logger.info("auto-generate: 적절한 클러스터 없음 (또는 데일리싱크 DB 미가용)")
            stats["news_skipped"] = 1
        else:
            text, meta = _compose_source_text("cluster", {"cluster_id": cid})
            result = generate_cards(text=text, tone=DEFAULT_TONE, brand=DEFAULT_BRAND)
            session_id = f"auto_news_{int(time.time())}"
            meta["tone"] = DEFAULT_TONE
            meta["generated_at"] = datetime.utcnow().isoformat()
            meta["auto_generated"] = True
            save_generated_session(session_id, result, text, DEFAULT_BRAND, meta=meta)
            _attach_publish_schedule(session_id, "daily_news")
            stats["news_created"] = 1
            logger.info(f"auto-news created: {session_id} cluster={cid}")
    except Exception as e:
        logger.exception(f"auto-news failed: {e}")
        stats["errors"].append(f"news: {e}")

    # ---- 2. 툴팁 카드 ----
    try:
        topic = pick_today_tooltip()
        if topic is None:
            stats["tooltip_skipped"] = 1
        else:
            body = {"tool": topic["label"], "topic": topic["subtopic"]}
            text, meta = _compose_source_text("tooltip", body)
            meta["topic_key"] = topic["key"]
            result = generate_cards(text=text, tone=DEFAULT_TONE, brand=DEFAULT_BRAND)
            session_id = f"auto_tooltip_{int(time.time())}"
            meta["tone"] = DEFAULT_TONE
            meta["generated_at"] = datetime.utcnow().isoformat()
            meta["auto_generated"] = True
            save_generated_session(session_id, result, text, DEFAULT_BRAND, meta=meta)
            _attach_publish_schedule(session_id, "tool_tip", topic_key=topic["key"])
            stats["tooltip_created"] = 1
            logger.info(f"auto-tooltip created: {session_id} topic={topic['key']}")
    except Exception as e:
        logger.exception(f"auto-tooltip failed: {e}")
        stats["errors"].append(f"tooltip: {e}")

    return stats


# ============================================================
# 잡 2) 매 15분 — 예약 시각 도래한 세션 헤드리스 발행
# ============================================================
def _list_due_sessions(now_utc: datetime | None = None) -> list[tuple[str, dict]]:
    """publish_status=scheduled AND scheduled_publish_at <= now 인 세션 목록."""
    now_utc = now_utc or datetime.utcnow()
    items = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = data.get("meta") or {}
        if meta.get("publish_status") != "scheduled":
            continue
        sched = meta.get("scheduled_publish_at")
        if not sched:
            continue
        try:
            sched_dt = datetime.fromisoformat(sched)
        except ValueError:
            continue
        if sched_dt <= now_utc:
            items.append((f.stem, data))
    return items


def _update_session_meta(session_id: str, patch: dict) -> None:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = dict(data.get("meta") or {})
    meta.update(patch)
    data["meta"] = meta
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def job_refresh_tokens() -> dict:
    """장기 토큰(Threads/Meta)을 갱신하고 `.env` + os.environ 동기화."""
    from services.token_refresh import refresh_all
    return refresh_all()


def job_publish_due() -> dict:
    """예약 시각이 도래한 세션을 헤드리스 브라우저로 렌더 후 IG 발행."""
    stats = {"checked": 0, "published": 0, "failed": 0, "skipped": 0}

    if not AUTO_PUBLISH:
        stats["skipped"] = 1
        logger.info("AUTO_PUBLISH=false — 자동 발행 비활성")
        return stats

    due = _list_due_sessions()
    stats["checked"] = len(due)
    if not due:
        return stats

    from headless_publish import render_and_publish

    for session_id, data in due:
        try:
            _update_session_meta(session_id, {"publish_status": "publishing"})
            media_id = render_and_publish(session_id)
            _update_session_meta(session_id, {
                "publish_status": "published",
                "published_at": datetime.utcnow().isoformat(),
                "ig_media_id": media_id,
            })
            stats["published"] += 1
            logger.info(f"auto-published {session_id} media={media_id}")
        except Exception as e:
            logger.exception(f"auto-publish failed {session_id}: {e}")
            _update_session_meta(session_id, {
                "publish_status": "failed",
                "publish_error": str(e)[:500],
            })
            stats["failed"] += 1
    return stats

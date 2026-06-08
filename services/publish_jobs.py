"""백그라운드 발행 잡 관리.

발행 API 가 동기 응답을 기다리지 않고 즉시 job_id 를 반환하도록 하는 레지스트리.
클라이언트는 `/api/publish/jobs/<job_id>` 를 폴링해서 단계별 진행 상황을 본다.

각 플랫폼(`services/{instagram,facebook,threads}.py`)의 `publish_carousel(progress_cb=...)`
콜백을 통해 단계 업데이트를 받음.

스레드 안전(threading.Lock). 잡은 인메모리 dict — 서버 재시작 시 휘발. TTL=1h.
"""
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import requests

logger = logging.getLogger(__name__)

# 발행 '실패'만 영구 기록 (성공 이력은 저장 안 함) — 나중에 오류코드 보고 고치기 위함.
_ERROR_LOG = Path(__file__).resolve().parent.parent / "data" / "publish_errors.jsonl"


def _log_publish_error(session_id: str, platform: str, caption: str,
                       image_urls: list[str], error: str) -> None:
    """발행 실패 1건을 data/publish_errors.jsonl 에 1줄로 append."""
    try:
        _ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "session_id": session_id,
            "platform": platform,
            "error": error,
            "caption": (caption or "")[:200],
            "image_urls": image_urls,
        }
        with _ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("publish error 기록 실패")

# 단계 → overall_percent 매핑 (한 플랫폼 내)
_PHASE_BASE = {
    "pending": 0,
    "uploading": 10,    # 10 ~ 70 (current/total 비례)
    "finalizing": 80,
    "publishing": 92,
    "done": 100,
    "skipped": 100,
    "error": 100,
}

_STEP_LABEL = {
    "pending": "대기",
    "uploading": "이미지 업로드",
    "finalizing": "캐러셀 마무리",
    "publishing": "게시 중",
    "done": "완료",
    "skipped": "스킵 (토큰 미설정)",
    "error": "실패",
}

_JOB_TTL_SEC = 3600
_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _gc():
    now = time.time()
    stale = [k for k, j in _jobs.items() if now - j.get("created_at_ts", now) > _JOB_TTL_SEC]
    for k in stale:
        _jobs.pop(k, None)


def _platform_percent(p: dict) -> int:
    """한 플랫폼의 진행률 0~100."""
    status = p.get("status", "pending")
    base = _PHASE_BASE.get(status, 0)
    if status == "uploading":
        cur, tot = p.get("current", 0), max(p.get("total", 1), 1)
        return base + int((70 - 10) * cur / tot)
    return base


def _recalc_overall(job: dict) -> None:
    plats = list(job.get("platforms", {}).values())
    if not plats:
        job["overall_percent"] = 0
        return
    job["overall_percent"] = int(sum(_platform_percent(p) for p in plats) / len(plats))


def get_job(job_id: str) -> dict | None:
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def _make_progress_cb(job_id: str, platform: str) -> Callable[[str, dict], None]:
    def cb(phase: str, info: dict) -> None:
        with _lock:
            j = _jobs.get(job_id)
            if not j:
                return
            p = j["platforms"].setdefault(platform, {})
            p["status"] = phase
            p["step_label"] = _STEP_LABEL.get(phase, phase)
            for k in ("current", "total", "error", "media_id", "permalink"):
                if k in info:
                    p[k] = info[k]
            _recalc_overall(j)
    return cb


def _check_image_urls(image_urls: list[str]) -> str | None:
    """첫 번째 이미지 URL에 HEAD 요청으로 접근 가능 여부 확인.

    접근 불가면 에러 메시지 반환, 정상이면 None.
    외부 CDN(Instagram 등)이 이미지를 가져올 수 없는 상황을 발행 전에 감지.
    """
    if not image_urls:
        return None
    url = image_urls[0]
    try:
        r = requests.head(url, timeout=10, allow_redirects=True)
        if r.status_code == 200:
            return None
        return (
            f"이미지 URL 접근 실패 (HTTP {r.status_code}). "
            f"터널이 살아있는지 확인하세요: {url}"
        )
    except requests.ConnectionError as e:
        return (
            f"이미지 URL에 연결할 수 없습니다 (DNS/연결 오류). "
            f"터널을 재시작하거나 SERVER_URL을 확인하세요.\n원인: {e}"
        )
    except requests.Timeout:
        return f"이미지 URL 응답 타임아웃 (10s). 터널 상태 확인: {url}"
    except Exception as e:
        return f"이미지 URL 확인 중 오류: {e}"


def _run(job_id: str, image_urls: list[str], caption: str) -> None:
    """워커 본체 — 등록된 플랫폼들을 순차 발행."""
    from services import instagram as ig_svc
    from services import facebook as fb_svc
    from services import threads as th_svc
    from services import tiktok as tk_svc

    svc = {
        "instagram": (ig_svc.is_configured, ig_svc.publish_carousel),
        "facebook":  (fb_svc.is_configured, fb_svc.publish_carousel),
        "threads":   (th_svc.is_configured, th_svc.publish_carousel),
        "tiktok":    (tk_svc.is_configured, tk_svc.publish_carousel),
    }

    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        platforms = list(job["platforms"].keys())
        session_id = job.get("session_id", "")

    # 발행 전 URL 접근 가능 여부 사전 검증
    url_err = _check_image_urls(image_urls)
    if url_err:
        logger.error(f"publish URL check failed: {url_err}")
        with _lock:
            j = _jobs.get(job_id)
            if j:
                for p in j["platforms"].values():
                    p["status"] = "error"
                    p["step_label"] = _STEP_LABEL["error"]
                    p["error"] = url_err
                j["status"] = "done"
                j["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _recalc_overall(j)
        _log_publish_error(session_id, "url_check", caption, image_urls, url_err)
        return

    for platform in platforms:
        is_cfg, publish_fn = svc[platform]
        cb = _make_progress_cb(job_id, platform)
        if not is_cfg():
            cb("skipped", {"error": "토큰 미설정"})
            continue
        try:
            result = publish_fn(image_urls, caption, progress_cb=cb)
            cb("done", {
                "media_id": result.get("media_id") or result.get("post_id"),
                "permalink": result.get("permalink"),
            })
        except Exception as e:
            logger.exception(f"publish {platform} failed")
            err = str(e)[:500]
            cb("error", {"error": err})
            # 실패만 영구 기록 — 나중에 오류코드 확인용
            _log_publish_error(session_id, platform, caption, image_urls, err)

    with _lock:
        j = _jobs.get(job_id)
        if j:
            j["status"] = "done"
            j["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _recalc_overall(j)


def start_publish_job(session_id: str, platforms: list[str],
                      image_urls: list[str], caption: str,
                      card_count: int) -> str:
    """백그라운드 worker 시작. 즉시 job_id 반환."""
    job_id = uuid.uuid4().hex[:12]
    now = time.time()

    job = {
        "job_id": job_id,
        "session_id": session_id,
        "status": "running",
        "card_count": card_count,
        "caption": caption,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "created_at_ts": now,
        "platforms": {p: {"status": "pending", "step_label": _STEP_LABEL["pending"]}
                      for p in platforms},
        "overall_percent": 0,
    }
    with _lock:
        _gc()
        _jobs[job_id] = job

    t = threading.Thread(
        target=_run,
        args=(job_id, image_urls, caption),
        name=f"publish-{job_id}",
        daemon=True,
    )
    t.start()
    return job_id


# ──────────────────────────────────────────────────────────────
# 직접 업로드 발행 (IMAGE + VIDEO 혼합 캐러셀)
# ──────────────────────────────────────────────────────────────

def _run_direct(job_id: str, items: list[dict], caption: str) -> None:
    """직접 업로드 미디어(혼합) 발행 워커."""
    from services import instagram as ig_svc
    from services import facebook as fb_svc
    from services import threads as th_svc

    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        platforms = list(job["platforms"].keys())
        scheduled_at = job.get("scheduled_at")
        session_id = job.get("session_id", "")

    # 예약 발행 — 지정 시각까지 대기 (최대 24시간)
    if scheduled_at:
        wait_sec = scheduled_at - time.time()
        if wait_sec > 0:
            with _lock:
                j = _jobs.get(job_id)
                if j:
                    for p in j["platforms"].values():
                        p["status"] = "scheduled"
                        p["step_label"] = "예약됨"
                    _recalc_overall(j)
            sleep_until = min(wait_sec, 86400)
            logger.info(f"direct job {job_id}: 예약 대기 {sleep_until:.0f}초")
            time.sleep(sleep_until)

    # Facebook 은 이미지만 지원
    image_items = [it for it in items if it.get("media_type", "IMAGE").upper() == "IMAGE"]

    for platform in platforms:
        cb = _make_progress_cb(job_id, platform)

        if platform == "instagram":
            is_cfg = ig_svc.is_configured
            publish_fn = lambda cap, pcb: ig_svc.publish_mixed_carousel(items, cap, progress_cb=pcb)
        elif platform == "threads":
            is_cfg = th_svc.is_configured
            publish_fn = lambda cap, pcb: th_svc.publish_mixed_carousel(items, cap, progress_cb=pcb)
        elif platform == "facebook":
            is_cfg = fb_svc.is_configured
            if not image_items:
                cb("skipped", {"error": "영상 전용 캐러셀은 Facebook 미지원 (이미지 없음)"})
                continue
            fb_urls = [it["url"] for it in image_items]
            publish_fn = lambda cap, pcb: fb_svc.publish_carousel(fb_urls, cap, progress_cb=pcb)
        else:
            cb("skipped", {"error": f"미지원 플랫폼: {platform}"})
            continue

        if not is_cfg():
            cb("skipped", {"error": "토큰 미설정"})
            continue

        try:
            result = publish_fn(caption, cb)
            cb("done", {
                "media_id": result.get("media_id") or result.get("post_id"),
                "permalink": result.get("permalink"),
            })
        except Exception as e:
            logger.exception(f"direct publish {platform} failed")
            err = str(e)[:500]
            cb("error", {"error": err})
            _log_publish_error(session_id, platform, caption,
                               [it["url"] for it in items], err)

    with _lock:
        j = _jobs.get(job_id)
        if j:
            j["status"] = "done"
            j["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _recalc_overall(j)


def start_direct_publish_job(session_id: str, platforms: list[str],
                             items: list[dict], caption: str,
                             scheduled_at: float | None = None) -> str:
    """직접 업로드 미디어 발행 잡 시작. 즉시 job_id 반환.

    items: [{"url": str, "media_type": "IMAGE"|"VIDEO"}, ...]
    scheduled_at: Unix 타임스탬프(초) — None 이면 즉시 발행.
    """
    job_id = uuid.uuid4().hex[:12]
    now = time.time()

    job = {
        "job_id": job_id,
        "session_id": session_id,
        "status": "running",
        "card_count": len(items),
        "caption": caption,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "created_at_ts": now,
        "is_direct": True,
        "scheduled_at": scheduled_at,
        "platforms": {p: {"status": "pending", "step_label": _STEP_LABEL["pending"]}
                      for p in platforms},
        "overall_percent": 0,
    }
    with _lock:
        _gc()
        _jobs[job_id] = job

    t = threading.Thread(
        target=_run_direct,
        args=(job_id, items, caption),
        name=f"direct-{job_id}",
        daemon=True,
    )
    t.start()
    return job_id

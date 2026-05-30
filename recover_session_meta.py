"""세션 meta 복구 — autoSave 버그로 잃어버린 cluster_id 추측 재주입.

추론 방법:
  1. session.lastSourceText 첫 줄 "주제: ..." 추출 → 데일리싱크 clusters.topic 정확 매칭
  2. 매칭 실패 시 cards[cover].data.title 로 fuzzy 매칭

실행: python recover_session_meta.py [--dry-run]
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
SESSIONS = ROOT / "sessions"
DAILY_DB = os.getenv("DAILYSYNC_DB_PATH", r"F:\ai-news-digest\ai-news-digest\data\app.db")

DRY = "--dry-run" in sys.argv


def _all_topics():
    """데일리싱크 모든 클러스터 (id, topic)."""
    if not Path(DAILY_DB).exists():
        return []
    con = sqlite3.connect(f"file:{DAILY_DB}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, topic FROM clusters WHERE topic IS NOT NULL").fetchall()
    finally:
        con.close()
    return rows  # [(id, topic), ...]


def _exact_match(topic_text: str, topics: list[tuple[int, str]]) -> int | None:
    if not topic_text:
        return None
    norm = topic_text.strip()
    for cid, t in topics:
        if t and t.strip() == norm:
            return cid
    return None


def _fuzzy_match(text: str, topics: list[tuple[int, str]]) -> int | None:
    """공통 글자 수 60% 이상 매칭."""
    if not text:
        return None
    text_norm = re.sub(r"[\s·​]+", "", text.strip())
    if not text_norm:
        return None
    best = None
    best_score = 0.0
    for cid, t in topics:
        if not t:
            continue
        t_norm = re.sub(r"[\s·​]+", "", t)
        common = sum(1 for ch in t_norm if ch in text_norm)
        score = common / max(1, len(t_norm))
        if score > best_score:
            best_score = score
            best = cid
    return best if best_score >= 0.6 else None


def _extract_topic_hint(session: dict) -> str:
    """lastSourceText '주제: ...' 행에서 토픽 추출."""
    txt = session.get("lastSourceText") or ""
    m = re.search(r"주제:\s*(.+)", txt)
    if m:
        return m.group(1).strip()
    # fallback — cover 카드의 title
    for c in session.get("cards", []):
        if c.get("type") == "cover":
            d = c.get("data") or {}
            return (d.get("title") or "").replace("\n", " ").strip()
    return ""


def main():
    topics = _all_topics()
    print(f"데일리싱크 클러스터 토픽 {len(topics)}건 로드")

    fixed = 0
    skipped = 0
    failed = 0
    for f in sorted(SESSIONS.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ✗ {f.name} 파싱 실패: {e}")
            failed += 1
            continue

        meta = dict(data.get("meta") or {})
        if meta.get("cluster_id"):
            skipped += 1
            continue
        if meta.get("kind") == "tool_tip" or meta.get("source") == "tooltip":
            # 툴팁 세션은 클러스터 없음이 정상
            skipped += 1
            continue

        topic_hint = _extract_topic_hint(data)
        if not topic_hint:
            print(f"  ? {f.name} 토픽 힌트 없음 — 복구 불가")
            failed += 1
            continue

        cid = _exact_match(topic_hint, topics) or _fuzzy_match(topic_hint, topics)
        if not cid:
            print(f"  ? {f.name} 매칭 실패 — hint='{topic_hint[:50]}'")
            failed += 1
            continue

        meta.setdefault("source", "dailysync")
        meta.setdefault("kind", "daily_news")
        meta["cluster_id"] = cid
        meta.setdefault("topic", topic_hint[:200])
        meta.setdefault("recovered_at", datetime.utcnow().isoformat())
        data["meta"] = meta

        if DRY:
            print(f"  [DRY] {f.name} → cluster_id={cid}")
        else:
            f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  ✓ {f.name} → cluster_id={cid}")
        fixed += 1

    print()
    print(f"복구: {fixed}건  /  스킵: {skipped}건  /  실패: {failed}건")
    if DRY:
        print("(dry-run 모드 — 실제 저장 안 됨. --dry-run 빼고 다시 실행)")


if __name__ == "__main__":
    main()

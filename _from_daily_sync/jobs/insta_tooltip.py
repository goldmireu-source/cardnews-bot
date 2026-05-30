"""인스타용 AI 툴 활용법 카드 생성.

흐름:
  1. data/tooltip_topics.yaml 로드
  2. 최근 30일 이내 게시·생성된 (tool, subtopic) 조합 제외
  3. 풀에서 1개 선정 (해시 기반 deterministic — 같은 날 두 번 돌려도 같은 결과)
  4. Claude 로 슬라이드(5~6장) + 캡션 + 해시태그 생성
  5. InstaPost(content_type="tooltip", status="draft") 저장
"""
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from config import Config
from models import db, InstaPost
from services.claude import generate_json

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
TOPICS_PATH = Path(__file__).resolve().parent.parent / "data" / "tooltip_topics.yaml"
RECENT_AVOID_DAYS = 30


def _load_pool() -> list[dict]:
    """topic pool 평면화: [{tool, label, subtopic, key}, ...]."""
    with open(TOPICS_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    flat = []
    for entry in raw or []:
        tool = entry.get("tool")
        label = entry.get("label") or tool
        for sub in entry.get("subtopics") or []:
            flat.append({
                "tool": tool,
                "label": label,
                "subtopic": sub,
                "key": f"{tool}::{hashlib.md5(sub.encode('utf-8')).hexdigest()[:8]}",
            })
    return flat


def _recent_used_keys(days: int = RECENT_AVOID_DAYS) -> set[str]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = db.session.query(InstaPost.topic_key).filter(
        InstaPost.content_type == "tooltip",
        InstaPost.created_at >= cutoff,
        InstaPost.topic_key.isnot(None),
    ).all()
    return {r[0] for r in rows}


def _pick_topic() -> dict | None:
    pool = _load_pool()
    used = _recent_used_keys()
    fresh = [p for p in pool if p["key"] not in used]
    if not fresh:
        # 모두 최근 사용 — 가장 오래된 것부터 다시 (사실상 used 무시)
        fresh = pool
    if not fresh:
        return None
    # 오늘(KST) 날짜 기반 deterministic 선택
    today_ord = datetime.now(KST).date().toordinal()
    return fresh[today_ord % len(fresh)]


def _build_prompt(topic: dict) -> str:
    return f"""당신은 한국어 AI 콘텐츠 크리에이터입니다.
아래 주제를 인스타그램 카드뉴스(5~6장)로 만드세요. 실전에서 바로 따라할 수 있는 팁이어야 합니다.

[주제]
도구: {topic['label']}
세부 주제: {topic['subtopic']}

[제작 가이드]
1. 표지(cover): 8~14자의 임팩트 문구. 누군가가 스크롤을 멈추게 만들 것.
2. 본문(steps): "어떤 상황에서 → 어떻게 → 결과/이점" 구조. 한 슬라이드당 한 단계.
3. 예시(example): 실제 프롬프트나 명령어 한 줄 포함. 따옴표 안에. 너무 길지 않게(60자 이내).
4. 마무리(cta): 저장·공유 유도. "프로필 링크" 같은 클리셰는 피하기.
5. 톤: 친근한 반말은 X. 정중한 평어체("~합니다", "~해요" 혼용). 자랑·과장 X.
6. 한 슬라이드 본문은 3~5줄, 한 줄 25자 이내.
7. 캡션은 200자 이내. 첫 줄에 후킹 한 문장.
8. 해시태그 8~12개, #AI #ChatGPT 같은 일반어 + 주제별 키워드.

[JSON 스키마 — 반드시 이대로]
{{
  "title": "전체 카드 제목 (40자 이내)",
  "slides": [
    {{"type": "cover", "headline": "임팩트 문구", "subhead": "도구명 또는 보조 문구"}},
    {{"type": "step", "label": "STEP 01", "headline": "단계 제목", "body": "본문 3~5줄"}},
    {{"type": "step", "label": "STEP 02", "headline": "단계 제목", "body": "본문 3~5줄"}},
    {{"type": "example", "label": "예시", "headline": "이렇게 써보세요", "body": "\\"실제 프롬프트 예시\\" — 효과 설명"}},
    {{"type": "cta", "headline": "마무리 한 줄", "body": "저장/공유 유도 1~2문장"}}
  ],
  "caption": "인스타 캡션 200자 이내",
  "hashtags": ["#ChatGPT", "#AI팁"]
}}
"""


def _make_post_from_topic(topic: dict) -> InstaPost | None:
    prompt = _build_prompt(topic)
    try:
        result = generate_json(prompt)
    except Exception as e:
        logger.exception(f"insta_tooltip Claude failed topic={topic['key']}: {e}")
        return None

    slides = result.get("slides") or []
    if len(slides) < 3:
        logger.warning(f"insta_tooltip slides too few topic={topic['key']}: {len(slides)}")
        return None

    title = (result.get("title") or f"{topic['label']} — {topic['subtopic']}")[:300]
    hashtags = result.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = [h.strip() for h in hashtags.split() if h.strip()]
    hashtags = [h if h.startswith("#") else f"#{h}" for h in hashtags[:15]]

    post = InstaPost(
        content_type="tooltip",
        topic_key=topic["key"],
        title=title,
        slides=slides,
        caption=result.get("caption") or "",
        hashtags=hashtags,
        status="draft",
        approved=not Config.INSTA_REQUIRE_APPROVAL,
    )
    return post


def generate_today_tooltip() -> dict:
    stats = {"picked": 0, "created": 0, "failed": 0}
    topic = _pick_topic()
    if topic is None:
        return stats
    stats["picked"] = 1

    post = _make_post_from_topic(topic)
    if post is None:
        stats["failed"] += 1
        return stats
    try:
        db.session.add(post)
        db.session.commit()
        stats["created"] += 1
        logger.info(f"insta_tooltip created post={post.id} topic={topic['key']} "
                    f"({topic['label']}: {topic['subtopic'][:30]}...)")
    except Exception:
        db.session.rollback()
        stats["failed"] += 1
        logger.exception(f"insta_tooltip db save failed topic={topic['key']}")
    return stats


if __name__ == "__main__":
    from app import create_app
    app = create_app(with_scheduler=False)
    with app.app_context():
        s = generate_today_tooltip()
        print(f"결과: {s}")

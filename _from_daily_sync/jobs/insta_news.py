"""인스타용 AI 뉴스 카드 콘텐츠 생성.

흐름:
  1. 오늘(KST) 발행된 클러스터 중 importance/매체수 기준 톱 N 선정
  2. 이미 InstaPost 로 변환된 클러스터는 제외
  3. 클러스터 데이터(topic/agreed_facts/divergences/summary_ko)를 Claude 로
     인스타 슬라이드(4~6장) + 캡션 + 해시태그로 재가공
  4. InstaPost(content_type="news", status="draft") 저장
"""
import logging
from datetime import datetime, timedelta, timezone

from config import Config
from models import db, Cluster, InstaPost
from services.claude import generate_json

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def _pick_top_clusters(limit: int = 1) -> list[Cluster]:
    """오늘(KST) 톱 클러스터 선정.

    조건:
      - summary_dirty=False (요약 완료)
      - hidden_at IS NULL (숨겨지지 않음)
      - 이미 InstaPost(news) 변환된 클러스터 제외
      - first_shown_date 가 오늘 또는 NULL
    정렬:
      - importance DESC, 매체 수 DESC, updated_at DESC
    """
    today_kst = datetime.now(KST).date()
    cutoff_utc = datetime.utcnow() - timedelta(days=2)

    # 이미 변환된 cluster_id
    used_ids = {
        row[0] for row in db.session.query(InstaPost.source_cluster_id)
        .filter(InstaPost.content_type == "news",
                InstaPost.source_cluster_id.isnot(None))
        .all()
    }

    q = Cluster.query.filter(
        Cluster.summary_dirty.is_(False),
        Cluster.hidden_at.is_(None),
        Cluster.updated_at >= cutoff_utc,
    )
    if used_ids:
        q = q.filter(~Cluster.id.in_(used_ids))

    candidates = q.order_by(
        Cluster.importance.desc(),
        Cluster.updated_at.desc(),
    ).limit(30).all()

    # 매체 수 기준 재정렬
    def _rank(c: Cluster) -> tuple:
        n_src = len({a.source_id for a in c.articles.all()})
        return (c.importance or 0, n_src, c.updated_at or datetime.min)

    candidates.sort(key=_rank, reverse=True)
    return candidates[:limit]


def _build_prompt(cluster: Cluster) -> str:
    members = cluster.articles.all()
    sources = sorted({a.source.name for a in members})
    facts_block = "\n".join(f"- {f}" for f in (cluster.agreed_facts or []))
    summary = cluster.summary_ko or ""

    return f"""당신은 인스타그램 운영 경험이 풍부한 한국어 콘텐츠 에디터입니다.
아래 AI 뉴스 클러스터를 인스타그램 카드뉴스(슬라이드 4~6장)로 재가공하세요.

[원본 데이터]
주제: {cluster.topic or '(없음)'}
중요도: {cluster.importance or 3}/5
보도 매체: {', '.join(sources)} ({len(sources)}개)

핵심 사실:
{facts_block or '(없음)'}

요약:
{summary}

[제작 가이드]
1. 첫 슬라이드(cover)는 스크롤을 멈추게 만드는 짧고 강한 후킹 문장 + 키워드 1~2개.
2. 본문 슬라이드(2~5장)는 한 슬라이드당 한 메시지. 본문은 짧은 문장 2~4개로 끊어쓰기.
3. 마지막 슬라이드(cta)는 저장·공유 유도 또는 다음 행동 제안.
4. 자극적·과장 표현 금지. 사실에 충실. 모르는 건 추가 금지.
5. 어려운 영어 약어가 나오면 한국어로 풀어쓰거나 괄호로 보충.
6. 캡션은 1~2 단락 (200자 이내), 인스타 톤. 첫 줄에 후킹.
7. 해시태그는 8~12개 (한글/영문 혼용 OK). 너무 일반적인 것(#일상)은 금지.

[JSON 스키마 — 반드시 이대로]
{{
  "hook": "표지에 쓸 13자 이내의 강한 후킹 문구",
  "title": "전체 카드 제목 (40자 이내, 인스타용)",
  "slides": [
    {{"type": "cover", "headline": "후킹 문구", "subhead": "보조 1줄"}},
    {{"type": "point", "label": "01", "headline": "핵심1", "body": "본문 2~3문장"}},
    {{"type": "point", "label": "02", "headline": "핵심2", "body": "본문 2~3문장"}},
    {{"type": "point", "label": "03", "headline": "핵심3", "body": "본문 2~3문장"}},
    {{"type": "cta", "headline": "마무리 한 줄", "body": "행동유도 1~2문장"}}
  ],
  "caption": "인스타 캡션 (200자 이내, 줄바꿈 OK)",
  "hashtags": ["#AI", "#OpenAI", "#인공지능"]
}}
"""


def _make_post_from_cluster(cluster: Cluster) -> InstaPost | None:
    prompt = _build_prompt(cluster)
    try:
        result = generate_json(prompt)
    except Exception as e:
        logger.exception(f"insta_news Claude failed cluster={cluster.id}: {e}")
        return None

    slides = result.get("slides") or []
    if not slides or len(slides) < 2:
        logger.warning(f"insta_news slides too few cluster={cluster.id}: {len(slides)}")
        return None

    title = (result.get("title") or cluster.topic or "오늘의 AI 뉴스")[:300]
    hashtags = result.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = [h.strip() for h in hashtags.split() if h.strip()]
    # # 누락 보정
    hashtags = [h if h.startswith("#") else f"#{h}" for h in hashtags[:15]]

    post = InstaPost(
        content_type="news",
        source_cluster_id=cluster.id,
        title=title,
        slides=slides,
        caption=result.get("caption") or "",
        hashtags=hashtags,
        status="draft",
        approved=not Config.INSTA_REQUIRE_APPROVAL,
    )
    return post


def generate_today_news_posts(limit: int = 1) -> dict:
    """오늘의 뉴스 카드 N개 생성 (기본 1개)."""
    stats = {"picked": 0, "created": 0, "failed": 0}
    clusters = _pick_top_clusters(limit=limit)
    stats["picked"] = len(clusters)

    for c in clusters:
        post = _make_post_from_cluster(c)
        if post is None:
            stats["failed"] += 1
            continue
        try:
            db.session.add(post)
            db.session.commit()
            stats["created"] += 1
            logger.info(f"insta_news created post={post.id} from cluster={c.id} "
                        f"(importance={c.importance}, slides={len(post.slides)})")
        except Exception:
            db.session.rollback()
            stats["failed"] += 1
            logger.exception(f"insta_news db save failed cluster={c.id}")

    return stats


if __name__ == "__main__":
    from app import create_app
    app = create_app(with_scheduler=False)
    with app.app_context():
        print(f"뉴스 카드 생성 시작 — 모델 {Config.CLAUDE_SUMMARY_MODEL}")
        s = generate_today_news_posts(limit=1)
        print(f"결과: {s}")

"""인스타 카드뉴스 파이프라인 잡들 — scheduler.py 에서 호출.

JobRun 으로 트래킹. _track 컨텍스트는 jobs/pipeline.py 에서 재사용.

잡 목록:
  - job_insta_generate_daily : 매일 07:30 KST — 뉴스 카드 + 툴팁 draft 1개씩 생성
  - job_insta_render_pending : 30분마다 — draft 일괄 렌더 → ready
  - job_insta_publish_due    : 1시간마다 — scheduled 시각 도래한 ready 게시
"""
import logging
from datetime import datetime, timedelta, timezone

from jobs.pipeline import _track
from models import db, InstaPost
from config import Config

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


def job_insta_generate_daily(triggered_by: str = "scheduler") -> dict:
    """오늘의 인스타 카드(뉴스 1 + 툴팁 1) draft 생성."""
    from jobs.insta_news import generate_today_news_posts
    from jobs.insta_tooltip import generate_today_tooltip

    with _track("insta_generate_daily", triggered_by) as stats:
        s_news = generate_today_news_posts(limit=1)
        s_tip = generate_today_tooltip()
        stats["news_created"] = s_news.get("created", 0)
        stats["news_failed"] = s_news.get("failed", 0)
        stats["tooltip_created"] = s_tip.get("created", 0)
        stats["tooltip_failed"] = s_tip.get("failed", 0)

        # 게시 시각 자동 예약 — 콘텐츠 타입별 기본 시간(KST) 으로
        today_kst = datetime.now(KST).date()
        news_dt = datetime.combine(today_kst, datetime.min.time()).replace(
            hour=Config.INSTA_NEWS_POST_HOUR, tzinfo=KST)
        tip_dt = datetime.combine(today_kst, datetime.min.time()).replace(
            hour=Config.INSTA_TOOLTIP_POST_HOUR, tzinfo=KST)

        # KST → UTC naive 로 변환 (DB 컬럼은 naive UTC)
        fresh = InstaPost.query.filter(
            InstaPost.status == "draft",
            InstaPost.scheduled_at.is_(None),
            InstaPost.created_at >= datetime.utcnow() - timedelta(hours=1),
        ).all()
        scheduled_count = 0
        for p in fresh:
            target_kst = news_dt if p.content_type == "news" else tip_dt
            # 이미 지난 시각이면 다음날로
            if target_kst < datetime.now(KST):
                target_kst += timedelta(days=1)
            p.scheduled_at = target_kst.astimezone(timezone.utc).replace(tzinfo=None)
            scheduled_count += 1
        if scheduled_count:
            db.session.commit()
        stats["scheduled"] = scheduled_count
        return stats


def job_insta_render_pending(triggered_by: str = "scheduler") -> dict:
    """draft 상태 InstaPost 를 Playwright 로 렌더링."""
    from jobs.insta_render import render_pending
    with _track("insta_render_pending", triggered_by) as stats:
        s = render_pending(limit=20)
        stats.update(s)
        return stats


def job_insta_publish_due(triggered_by: str = "scheduler") -> dict:
    """예약 시각이 지난 ready 상태 InstaPost 를 인스타에 게시.

    조건:
      - status == 'ready' 또는 'scheduled'
      - scheduled_at <= now (UTC) — scheduled_at NULL 인 경우는 자동 게시 안 함
      - approved == True (Config.INSTA_REQUIRE_APPROVAL=False 면 모두 자동 True)
      - image_paths 가 존재
    """
    from services.instagram import publish_carousel, InstagramError

    with _track("insta_publish_due", triggered_by) as stats:
        stats["attempted"] = 0
        stats["posted"] = 0
        stats["failed"] = 0
        stats["skipped_unconfigured"] = 0

        if not (Config.IG_USER_ID and Config.IG_ACCESS_TOKEN and Config.PUBLIC_BASE_URL):
            stats["skipped_unconfigured"] = 1
            logger.info("insta publish skipped — Graph API 미설정")
            return stats

        now_utc = datetime.utcnow()
        due = InstaPost.query.filter(
            InstaPost.status.in_(["ready", "scheduled"]),
            InstaPost.scheduled_at.isnot(None),
            InstaPost.scheduled_at <= now_utc,
            InstaPost.approved.is_(True),
        ).order_by(InstaPost.scheduled_at.asc()).limit(5).all()

        for post in due:
            if not post.image_paths:
                continue
            stats["attempted"] += 1
            try:
                post.status = "posting"
                db.session.commit()
                caption = post.caption or ""
                if post.hashtags:
                    caption = (caption + "\n\n" + " ".join(post.hashtags)).strip()
                result = publish_carousel(post.image_paths, caption)
                post.status = "posted"
                post.posted_at = datetime.utcnow()
                post.ig_media_id = result.get("media_id")
                post.ig_permalink = result.get("permalink")
                post.error = None
                db.session.commit()
                stats["posted"] += 1
                logger.info(f"insta posted post={post.id} media={post.ig_media_id}")
            except InstagramError as e:
                post.status = "failed"
                post.error = str(e)[:1000]
                db.session.commit()
                stats["failed"] += 1
                logger.exception(f"insta publish failed post={post.id}: {e}")
            except Exception as e:
                post.status = "failed"
                post.error = f"unexpected: {e}"[:1000]
                db.session.commit()
                stats["failed"] += 1
                logger.exception(f"insta publish unexpected post={post.id}: {e}")
        return stats

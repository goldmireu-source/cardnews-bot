"""Playwright 로 인스타 카드 슬라이드 → PNG 렌더링.

Flask test_client 로 HTML 을 생성하고 Playwright 의 set_content() 로 로드.
별도 서버 없이 in-process 로 동작.

요구사항: pip install playwright; playwright install chromium
"""
import logging
import os
from datetime import datetime
from pathlib import Path

from flask import current_app

from models import db, InstaPost

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static" / "instagram"
VIEWPORT = {"width": 1080, "height": 1080}


def _render_post_slides(post: InstaPost, page) -> list[str]:
    """단일 InstaPost 의 모든 슬라이드를 PNG 로 저장 → 경로 리스트 반환."""
    client = current_app.test_client()
    slides = post.slides or []
    out_dir = STATIC_DIR / str(post.id)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = []
    for i in range(1, len(slides) + 1):
        r = client.get(f"/insta/preview/{post.id}/{i}")
        if r.status_code != 200:
            raise RuntimeError(f"preview HTTP {r.status_code} post={post.id} slide={i}")
        html = r.data.decode("utf-8")

        page.set_content(html, wait_until="networkidle")
        # 웹 폰트가 늦게 적용되는 문제 회피
        page.evaluate("document.fonts.ready")

        out_path = out_dir / f"slide{i}.png"
        page.screenshot(
            path=str(out_path),
            clip={"x": 0, "y": 0, "width": 1080, "height": 1080},
            omit_background=False,
        )
        # 정적 URL 경로 (Flask static 라우트 기준)
        url_path = f"/static/instagram/{post.id}/slide{i}.png"
        paths.append(url_path)
        logger.info(f"render post={post.id} slide={i} -> {out_path.name}")

    return paths


def render_pending(limit: int = 10) -> dict:
    """status='draft' 인 InstaPost 들을 렌더링 → status='ready'."""
    from playwright.sync_api import sync_playwright

    stats = {"total": 0, "rendered": 0, "failed": 0}

    drafts = InstaPost.query.filter_by(status="draft").order_by(InstaPost.id).limit(limit).all()
    stats["total"] = len(drafts)
    if not drafts:
        return stats

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=1,
                color_scheme="dark",
            )
            page = ctx.new_page()

            for post in drafts:
                try:
                    paths = _render_post_slides(post, page)
                    post.image_paths = paths
                    post.rendered_at = datetime.utcnow()
                    post.status = "ready"
                    db.session.commit()
                    stats["rendered"] += 1
                except Exception as e:
                    db.session.rollback()
                    post.status = "failed"
                    post.error = f"render: {e}"[:1000]
                    db.session.commit()
                    stats["failed"] += 1
                    logger.exception(f"render failed post={post.id}: {e}")
        finally:
            browser.close()

    return stats


def render_one(post_id: int) -> bool:
    """단일 포스트 렌더 — 관리자 수동 트리거용."""
    from playwright.sync_api import sync_playwright

    post = InstaPost.query.get(post_id)
    if not post:
        return False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport=VIEWPORT)
            page = ctx.new_page()
            try:
                paths = _render_post_slides(post, page)
                post.image_paths = paths
                post.rendered_at = datetime.utcnow()
                if post.status in ("draft", "failed"):
                    post.status = "ready"
                db.session.commit()
                return True
            except Exception as e:
                db.session.rollback()
                post.status = "failed"
                post.error = f"render: {e}"[:1000]
                db.session.commit()
                logger.exception(f"render_one failed post={post.id}: {e}")
                return False
        finally:
            browser.close()


if __name__ == "__main__":
    from app import create_app
    app = create_app(with_scheduler=False)
    with app.app_context():
        print(f"렌더링 시작 — static dir: {STATIC_DIR}")
        s = render_pending(limit=10)
        print(f"결과: {s}")

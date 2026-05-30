"""인스타 카드뉴스 관련 라우트.

분리 목적: web/routes.py 가 이미 500+ 줄이라 별 blueprint 로 격리.
공개 라우트:
  /insta/preview/<post_id>/<slide_n>   — 한 슬라이드 HTML 렌더 (Playwright 가 스크린샷)
  /insta/preview/<post_id>             — 전체 슬라이드 한 페이지 (관리자 확인용)
관리자 라우트:
  /admin/instagram                     — 큐 목록
  /admin/insta/<id>                    — 단건 상세
  /api/insta/<id>/{approve,reject,reschedule,render,post-now,regenerate}
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from flask import Blueprint, abort, render_template, jsonify, request, Response, current_app

from config import Config
from models import db, InstaPost

logger = logging.getLogger(__name__)

bp_insta = Blueprint("insta", __name__)

KST = timezone(timedelta(hours=9))


def _get_post_or_404(post_id: int) -> InstaPost:
    post = InstaPost.query.get(post_id)
    if not post:
        abort(404)
    return post


# ---------- 슬라이드 단건 (Playwright 스크린샷용) ----------
@bp_insta.route("/insta/preview/<int:post_id>/<int:slide_n>")
def preview_slide(post_id: int, slide_n: int):
    post = _get_post_or_404(post_id)
    slides = post.slides or []
    if slide_n < 1 or slide_n > len(slides):
        abort(404)
    slide = slides[slide_n - 1]
    return render_template(
        "insta/card.html",
        post=post,
        slide=slide,
        slide_index=slide_n - 1,
        total=len(slides),
        brand_name=Config.INSTA_BRAND_NAME,
    )


# ---------- 슬라이드 전체 (관리자 미리보기) ----------
@bp_insta.route("/insta/preview/<int:post_id>")
def preview_all(post_id: int):
    post = _get_post_or_404(post_id)
    slides = post.slides or []
    if not slides:
        abort(404)
    # 가벼운 그리드 뷰 — iframe으로 각 슬라이드를 끼워넣음
    items_html = "\n".join(
        f"""
        <div class="item">
          <iframe src="/insta/preview/{post.id}/{i+1}"
                  width="1080" height="1080" frameborder="0"
                  style="transform:scale(0.35);transform-origin:top left;"></iframe>
          <div class="lbl">{i+1} / {len(slides)} — {s.get('type','?')}</div>
        </div>
        """ for i, s in enumerate(slides)
    )
    html = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>preview — {post.title}</title>
<style>
 body {{ background:#222; color:#eee; font-family:-apple-system,BlinkMacSystemFont,sans-serif;
        margin:0; padding:20px; }}
 h1 {{ font-size:18px; margin:0 0 8px; }}
 .meta {{ font-size:13px; opacity:0.7; margin-bottom:20px; }}
 .grid {{ display:flex; flex-wrap:wrap; gap:24px; }}
 .item {{ width:380px; height:430px; overflow:hidden; background:#000;
         border:1px solid #444; border-radius:8px; position:relative; }}
 .item iframe {{ pointer-events:none; }}
 .lbl {{ position:absolute; bottom:0; left:0; right:0; padding:6px 10px;
        background:rgba(0,0,0,0.7); font-size:12px; }}
 .caption {{ margin-top:24px; padding:16px; background:#333; border-radius:8px;
            font-size:14px; line-height:1.6; max-width:1000px; white-space:pre-wrap; }}
 .tags {{ margin-top:8px; font-size:13px; color:#aaa; }}
</style></head><body>
<h1>{post.title}</h1>
<div class="meta">#{post.id} · {post.content_type} · status={post.status} · {len(slides)} slides</div>
<div class="grid">{items_html}</div>
<div class="caption">{post.caption or '(no caption)'}</div>
<div class="tags">{' '.join(post.hashtags or [])}</div>
</body></html>"""
    return Response(html, mimetype="text/html")


# ---------- 관리자 액션 API ----------
@bp_insta.route("/insta/api/<int:post_id>/approve", methods=["POST"])
def api_approve(post_id: int):
    post = _get_post_or_404(post_id)
    post.approved = True
    db.session.commit()
    return jsonify({"ok": True, "id": post.id, "approved": True})


@bp_insta.route("/insta/api/<int:post_id>/reject", methods=["POST"])
def api_reject(post_id: int):
    post = _get_post_or_404(post_id)
    post.status = "failed"
    post.error = "rejected by admin"
    db.session.commit()
    return jsonify({"ok": True, "id": post.id, "status": post.status})


@bp_insta.route("/insta/api/<int:post_id>/schedule", methods=["POST"])
def api_schedule(post_id: int):
    post = _get_post_or_404(post_id)
    iso = (request.json or {}).get("scheduled_at") if request.is_json else request.form.get("scheduled_at")
    if not iso:
        return jsonify({"ok": False, "error": "scheduled_at required"}), 400
    try:
        post.scheduled_at = datetime.fromisoformat(iso)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid datetime"}), 400
    if post.status == "ready":
        post.status = "scheduled"
    db.session.commit()
    return jsonify({"ok": True, "id": post.id, "scheduled_at": post.scheduled_at.isoformat()})


@bp_insta.route("/insta/api/<int:post_id>/edit", methods=["POST"])
def api_edit(post_id: int):
    """캡션/해시태그 인플레이스 수정."""
    post = _get_post_or_404(post_id)
    payload = request.json or request.form
    if "caption" in payload:
        post.caption = (payload.get("caption") or "")[:5000]
    if "hashtags" in payload:
        raw = payload.get("hashtags") or ""
        if isinstance(raw, str):
            tags = [t.strip() for t in raw.split() if t.strip()]
        else:
            tags = list(raw)
        post.hashtags = [t if t.startswith("#") else f"#{t}" for t in tags[:20]]
    db.session.commit()
    return jsonify({"ok": True, "id": post.id, "caption": post.caption, "hashtags": post.hashtags})


@bp_insta.route("/insta/api/<int:post_id>/render", methods=["POST"])
def api_render(post_id: int):
    """수동 렌더링 — 동기 실행 (Playwright 호출은 보통 수 초)."""
    from jobs.insta_render import render_one
    post = _get_post_or_404(post_id)
    ok = render_one(post.id)
    db.session.refresh(post)
    return jsonify({"ok": ok, "id": post.id, "status": post.status,
                    "image_paths": post.image_paths, "error": post.error})


def _post_now_background(app, post_id: int):
    """백그라운드로 인스타 업로드 — 보통 30초~몇 분 걸려서 비동기."""
    with app.app_context():
        from services.instagram import publish_carousel, InstagramError
        post = InstaPost.query.get(post_id)
        if not post:
            return
        if not post.image_paths:
            post.status = "failed"
            post.error = "이미지 미렌더링"
            db.session.commit()
            return
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
            logger.info(f"insta posted post={post.id} media={post.ig_media_id}")
        except InstagramError as e:
            post.status = "failed"
            post.error = str(e)[:1000]
            db.session.commit()
            logger.exception(f"insta post failed post={post.id}: {e}")
        except Exception as e:
            post.status = "failed"
            post.error = f"unexpected: {e}"[:1000]
            db.session.commit()
            logger.exception(f"insta post unexpected post={post.id}: {e}")


@bp_insta.route("/insta/api/<int:post_id>/post-now", methods=["POST"])
def api_post_now(post_id: int):
    """즉시 게시 — 백그라운드 스레드로 위임."""
    post = _get_post_or_404(post_id)
    if not post.image_paths:
        return jsonify({"ok": False, "error": "이미지가 렌더링되지 않았습니다"}), 400
    if post.status == "posting":
        return jsonify({"ok": False, "error": "이미 게시 중"}), 409
    app = current_app._get_current_object()  # threading 용 실제 앱 객체
    threading.Thread(target=_post_now_background, args=(app, post.id), daemon=True).start()
    return jsonify({"ok": True, "id": post.id, "status": "posting"})


# ---------- 관리자 큐 페이지 ----------
@bp_insta.route("/admin/instagram")
def admin_queue():
    """인스타 큐 페이지 — 상태별 필터, 미리보기, 액션."""
    status_filter = request.args.get("status", "")
    q = InstaPost.query
    if status_filter:
        q = q.filter(InstaPost.status == status_filter)
    posts = q.order_by(InstaPost.created_at.desc()).limit(100).all()

    counts = dict(db.session.query(InstaPost.status, db.func.count(InstaPost.id))
                  .group_by(InstaPost.status).all())

    ig_configured = bool(Config.IG_USER_ID and Config.IG_ACCESS_TOKEN
                         and Config.PUBLIC_BASE_URL)

    return render_template(
        "insta/admin_queue.html",
        posts=posts,
        counts=counts,
        status_filter=status_filter,
        ig_configured=ig_configured,
        kst_offset=timedelta(hours=9),
        require_approval=Config.INSTA_REQUIRE_APPROVAL,
    )


@bp_insta.route("/admin/instagram/<int:post_id>")
def admin_detail(post_id: int):
    post = _get_post_or_404(post_id)
    return render_template(
        "insta/admin_detail.html",
        post=post,
        kst_offset=timedelta(hours=9),
    )


# ---------- 수동 생성 트리거 ----------
@bp_insta.route("/insta/api/generate/<kind>", methods=["POST"])
def api_generate(kind: str):
    """draft 생성 트리거 (수동). kind: 'news' | 'tooltip'."""
    if kind == "news":
        from jobs.insta_news import generate_today_news_posts
        stats = generate_today_news_posts(limit=int(request.args.get("limit", 1)))
    elif kind == "tooltip":
        from jobs.insta_tooltip import generate_today_tooltip
        stats = generate_today_tooltip()
    else:
        return jsonify({"ok": False, "error": "unknown kind"}), 400
    return jsonify({"ok": True, "kind": kind, "stats": stats})

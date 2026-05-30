"""Facebook 페이지 캐러셀 게시 (멀티 사진 포스트).

흐름:
  1. 각 이미지를 페이지에 published=false 로 업로드 → 각 photo_id 받음
  2. /{page-id}/feed POST 에 attached_media=[{media_fbid: id}, ...] + message=caption
  3. 게시 완료 → post id 반환

전제:
  - Meta 앱이 `pages_manage_posts` 권한 보유
  - FB_PAGE_ACCESS_TOKEN 이 해당 페이지의 long-lived page token
  - 이미지 URL 은 외부에서 https 로 접근 가능해야 함

ENV:
  - FB_PAGE_ID
  - FB_PAGE_ACCESS_TOKEN
  - SERVER_URL (이미지 fetch 용 — 인스타 발행과 공유)
"""
import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v21.0"

# 게시 후 응답까지 폴링 (선택 — 페이스북은 보통 즉시 반환)
_POLL_INTERVAL = 2
_POLL_MAX = 15


class FacebookError(RuntimeError):
    pass


def _env() -> tuple[str, str, str]:
    page_id = os.getenv("FB_PAGE_ID", "").strip()
    token = os.getenv("FB_PAGE_ACCESS_TOKEN", "").strip()
    server = os.getenv("SERVER_URL", "http://localhost:5050").rstrip("/")
    if not page_id or not token:
        raise FacebookError(
            "FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN 미설정 (docs/POSTING_SETUP.md 참고)"
        )
    return page_id, token, server


def _post(path: str, data: dict, token: str) -> dict:
    data = {**data, "access_token": token}
    r = requests.post(f"{GRAPH}/{path.lstrip('/')}", data=data, timeout=30)
    if r.status_code >= 400:
        raise FacebookError(f"FB POST {path} HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def is_configured() -> bool:
    return bool(os.getenv("FB_PAGE_ID", "").strip()
                and os.getenv("FB_PAGE_ACCESS_TOKEN", "").strip())


def publish_carousel(image_urls: list[str], caption: str = "",
                     progress_cb=None) -> dict:
    """페이스북 페이지에 멀티 사진 포스트.

    image_urls: 외부에서 https 로 접근 가능한 절대 URL 리스트 (최대 10장).
    caption: 게시물 메시지 (해시태그 포함 가능).
    progress_cb(phase, info): 단계 콜백.

    return: {"post_id": ..., "permalink": ...}
    """
    page_id, token, server = _env()
    if not image_urls:
        raise FacebookError("이미지가 없습니다")
    if len(image_urls) > 10:
        image_urls = image_urls[:10]

    if not server.startswith("https://"):
        raise FacebookError(
            "SERVER_URL 이 https 가 아닙니다. ngrok 또는 Cloudflare Tunnel 로 외부 노출 필요."
        )

    logger.info(f"FB carousel publish start: {len(image_urls)} images")
    total = len(image_urls)
    if progress_cb:
        progress_cb("uploading", {"current": 0, "total": total})

    # 1) 각 이미지 published=false 로 업로드 → fbid 받기
    media_ids: list[str] = []
    for i, url in enumerate(image_urls, 1):
        res = _post(f"{page_id}/photos", {
            "url": url,
            "published": "false",
        }, token)
        fbid = res.get("id")
        if not fbid:
            raise FacebookError(f"FB photo upload 실패 ({i}/{len(image_urls)}): {res}")
        media_ids.append(fbid)
        logger.info(f"  FB photo {i}/{len(image_urls)} id={fbid}")
        if progress_cb:
            progress_cb("uploading", {"current": i, "total": total})

    # 2) feed 에 attached_media 로 묶어서 게시
    # attached_media 는 JSON 인코딩된 문자열 배열
    import json as _json
    attached = _json.dumps([{"media_fbid": m} for m in media_ids])
    if progress_cb:
        progress_cb("publishing", {})
    res = _post(f"{page_id}/feed", {
        "message": caption or "",
        "attached_media": attached,
    }, token)
    post_id = res.get("id")
    if not post_id:
        raise FacebookError(f"FB feed publish 응답에 id 없음: {res}")
    logger.info(f"  FB feed post id={post_id}")

    # 3) permalink 조회 (선택)
    permalink = None
    try:
        r = requests.get(
            f"{GRAPH}/{post_id}",
            params={"fields": "permalink_url", "access_token": token},
            timeout=10,
        )
        if r.ok:
            permalink = r.json().get("permalink_url")
    except Exception:
        pass

    return {"post_id": post_id, "permalink": permalink}

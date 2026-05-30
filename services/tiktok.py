"""TikTok Content Posting API — PHOTO 모드 캐러셀 발행.

Base URL: https://open.tiktokapis.com/v2

흐름 (PHOTO Direct Post):
  1. POST /post/publish/content/init/  → publish_id
       body: media_type=PHOTO, post_mode=DIRECT_POST,
             post_info={title, description, privacy_level, ...},
             source_info={source=PULL_FROM_URL, photo_images=[...], photo_cover_index}
  2. POST /post/publish/status/fetch/  body={publish_id}
       응답 status: PROCESSING_DOWNLOAD | PROCESSING_UPLOAD |
                    SEND_TO_USER_INBOX | PUBLISH_COMPLETE | FAILED

전제:
  - 별도 TikTok 앱 (Content Posting API)
  - scope: video.publish
  - PULL_FROM_URL 도메인은 TikTok 콘솔에 검증 등록되어 있어야 함

ENV:
  - TIKTOK_ACCESS_TOKEN
  - TIKTOK_OPEN_ID         (현재는 검증용으로만 — 발행 호출엔 token 만 사용)
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

API = "https://open.tiktokapis.com/v2"

_POLL_INTERVAL = 3
_POLL_MAX = 30  # 최대 ~90초

# privacy_level — 본인 계정 운영용 기본값
_DEFAULT_PRIVACY = os.getenv("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY")

_TITLE_MAX = 90
_DESC_MAX = 4000


class TikTokError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.getenv("TIKTOK_ACCESS_TOKEN", "").strip())


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _split_caption(caption: str) -> tuple[str, str]:
    """viral caption 을 title (90자) + description (4000자) 으로 분할.

    첫 줄을 title 로, 나머지 전체를 description 으로. title 이 너무 길면 잘라서 …
    """
    caption = (caption or "").strip()
    if not caption:
        return "", ""
    parts = caption.split("\n", 1)
    raw_title = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if len(raw_title) > _TITLE_MAX:
        cut = raw_title[:_TITLE_MAX - 1]
        sp = cut.rfind(" ")
        if sp > _TITLE_MAX * 0.6:
            cut = cut[:sp]
        title = cut.rstrip() + "…"
        rest = (raw_title[len(cut):].lstrip() + "\n" + rest).strip()
    else:
        title = raw_title

    description = rest[:_DESC_MAX]
    return title, description


def _wait_for_publish(publish_id: str, token: str) -> dict:
    """상태 폴링 — PUBLISH_COMPLETE / SEND_TO_USER_INBOX 까지 대기."""
    last_status = ""
    for _ in range(_POLL_MAX):
        time.sleep(_POLL_INTERVAL)
        try:
            r = requests.post(
                f"{API}/post/publish/status/fetch/",
                headers=_headers(token),
                json={"publish_id": publish_id},
                timeout=15,
            )
        except requests.RequestException as e:
            last_status = f"network: {e}"
            continue
        if r.status_code >= 400:
            last_status = f"HTTP {r.status_code}: {r.text[:300]}"
            continue
        body = r.json() or {}
        data = body.get("data") or {}
        status = data.get("status", "")
        last_status = status
        if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return data
        if status == "FAILED":
            reason = data.get("fail_reason") or body.get("error", {})
            raise TikTokError(f"TikTok 발행 실패: {reason}")
    raise TikTokError(f"TikTok 상태 폴링 타임아웃 (last={last_status})")


def publish_carousel(image_urls: list[str], caption: str = "",
                     progress_cb=None) -> dict:
    """TikTok 에 PHOTO 캐러셀 게시.

    image_urls: 검증된 도메인의 https URL 리스트 (최대 35장).
    return: {"media_id": publish_id, "permalink": None}  (TikTok 은 permalink 즉시 조회 불가)
    """
    token = os.getenv("TIKTOK_ACCESS_TOKEN", "").strip()
    if not token:
        raise TikTokError("TIKTOK_ACCESS_TOKEN 미설정 (docs/POSTING_SETUP.md 3-G 참고)")
    if not image_urls:
        raise TikTokError("이미지가 없습니다")
    if len(image_urls) > 35:
        image_urls = image_urls[:35]

    total = len(image_urls)
    title, description = _split_caption(caption)
    logger.info(f"TikTok publish start: {total} photos, title={len(title)}자, desc={len(description)}자")

    # 1) init — TikTok 이 URL fetch 시작
    if progress_cb:
        progress_cb("uploading", {"current": 0, "total": total})
    try:
        r = requests.post(
            f"{API}/post/publish/content/init/",
            headers=_headers(token),
            json={
                "media_type": "PHOTO",
                "post_mode": "DIRECT_POST",
                "post_info": {
                    "title": title,
                    "description": description,
                    "privacy_level": _DEFAULT_PRIVACY,
                    "disable_comment": False,
                    "auto_add_music": False,
                    "brand_content_toggle": False,
                    "brand_organic_toggle": False,
                },
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "photo_images": image_urls,
                    "photo_cover_index": 0,
                },
            },
            timeout=30,
        )
    except requests.RequestException as e:
        raise TikTokError(f"TikTok init 네트워크 오류: {e}")
    if r.status_code >= 400:
        raise TikTokError(f"TikTok init HTTP {r.status_code}: {r.text[:500]}")

    body = r.json() or {}
    err = (body.get("error") or {})
    if err.get("code") and err.get("code") != "ok":
        raise TikTokError(f"TikTok init 에러 {err.get('code')}: {err.get('message')}")

    publish_id = (body.get("data") or {}).get("publish_id")
    if not publish_id:
        raise TikTokError(f"TikTok init 응답에 publish_id 없음: {body}")

    # progress 단계: TikTok 은 백엔드가 URL fetch 후 한꺼번에 처리 →
    # uploading 표시는 잠깐, finalizing 으로 넘어가서 폴링
    if progress_cb:
        progress_cb("uploading", {"current": total, "total": total})
        progress_cb("finalizing", {})

    # 2) 상태 폴링
    if progress_cb:
        progress_cb("publishing", {})
    _wait_for_publish(publish_id, token)

    logger.info(f"TikTok published publish_id={publish_id}")
    return {"media_id": publish_id, "permalink": None}


def refresh_token() -> dict | None:
    """refresh_token 으로 access_token + 새 refresh_token 재발급.

    응답에 새 refresh_token 도 함께 오면 그것도 갱신해야 함 (호출자 책임).
    return: {"access_token": ..., "refresh_token": ..., "expires_in": ...} 또는 None
    """
    client_key = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
    refresh = os.getenv("TIKTOK_REFRESH_TOKEN", "").strip()
    if not (client_key and client_secret and refresh):
        return None
    try:
        r = requests.post(
            "https://open.tiktokapis.com/v2/oauth/token/",
            data={
                "client_key": client_key,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
    except requests.RequestException:
        logger.exception("TikTok refresh request failed")
        return None
    if not r.ok:
        logger.warning(f"TikTok refresh HTTP {r.status_code}: {r.text[:300]}")
        return None
    body = r.json() or {}
    if not body.get("access_token"):
        logger.warning(f"TikTok refresh body has no access_token: {body}")
        return None
    return body

"""Threads 캐러셀 게시.

API 기반: https://graph.threads.net/v1.0 (인스타·페이스북과 다른 base URL)

흐름:
  1. 각 이미지마다 POST /{threads-user-id}/threads
     media_type=IMAGE, image_url=..., is_carousel_item=true → child container id
  2. POST /{threads-user-id}/threads (media_type=CAROUSEL, children=ids, text=caption)
     → carousel container id
  3. POST /{threads-user-id}/threads_publish (creation_id) → 게시
  4. 폴링 — 컨테이너 status 가 FINISHED 될 때까지

전제:
  - Threads API 별도 앱 등록 (인스타랑 같은 앱 못 씀)
  - permissions: threads_basic, threads_content_publish
  - long-lived user token (60일, 사용 시 자동 갱신)

ENV:
  - THREADS_USER_ID
  - THREADS_ACCESS_TOKEN
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

GRAPH = "https://graph.threads.net/v1.0"

_POLL_INTERVAL = 2
_POLL_MAX = 30


class ThreadsError(RuntimeError):
    pass


def _env() -> tuple[str, str]:
    user_id = os.getenv("THREADS_USER_ID", "").strip()
    token = os.getenv("THREADS_ACCESS_TOKEN", "").strip()
    if not user_id or not token:
        raise ThreadsError(
            "THREADS_USER_ID / THREADS_ACCESS_TOKEN 미설정 (docs/POSTING_SETUP.md 참고)"
        )
    return user_id, token


def is_configured() -> bool:
    return bool(os.getenv("THREADS_USER_ID", "").strip()
                and os.getenv("THREADS_ACCESS_TOKEN", "").strip())


def _post(path: str, data: dict, token: str) -> dict:
    data = {**data, "access_token": token}
    r = requests.post(f"{GRAPH}/{path.lstrip('/')}", data=data, timeout=30)
    if r.status_code >= 400:
        raise ThreadsError(f"Threads POST {path} HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def _get(path: str, params: dict, token: str) -> dict:
    params = {**params, "access_token": token}
    r = requests.get(f"{GRAPH}/{path.lstrip('/')}", params=params, timeout=30)
    if r.status_code >= 400:
        raise ThreadsError(f"Threads GET {path} HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def _wait_for_container(container_id: str, token: str) -> None:
    """컨테이너가 FINISHED 가 될 때까지 폴링."""
    for _ in range(_POLL_MAX):
        time.sleep(_POLL_INTERVAL)
        info = _get(container_id, {"fields": "status,error_message"}, token)
        status = info.get("status")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise ThreadsError(f"Threads container {container_id} status={status}: {info}")
    raise ThreadsError(f"Threads container {container_id} 폴링 타임아웃")


_THREADS_CAPTION_MAX = 500


def _truncate_for_threads(caption: str) -> str:
    """500자 한도 — 단어 경계에서 자르고 마지막 줄에 …."""
    if len(caption) <= _THREADS_CAPTION_MAX:
        return caption
    cut = caption[:_THREADS_CAPTION_MAX - 1]
    sp = cut.rfind(" ")
    nl = cut.rfind("\n")
    boundary = max(sp, nl)
    if boundary > _THREADS_CAPTION_MAX * 0.6:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def publish_carousel(image_urls: list[str], caption: str = "",
                     progress_cb=None) -> dict:
    """Threads 에 멀티 이미지 캐러셀 게시.

    image_urls: 외부에서 https 로 접근 가능한 절대 URL 리스트 (최대 20장).
    progress_cb(phase, info): 단계 콜백.
    return: {"media_id": ..., "permalink": ...}
    """
    user_id, token = _env()
    if not image_urls:
        raise ThreadsError("이미지가 없습니다")
    if len(image_urls) > 20:
        image_urls = image_urls[:20]

    caption = _truncate_for_threads(caption or "")

    logger.info(f"Threads carousel publish start: {len(image_urls)} images")
    total = len(image_urls)
    if progress_cb:
        progress_cb("uploading", {"current": 0, "total": total})

    # 1) 각 이미지를 child 컨테이너로
    children: list[str] = []
    for i, url in enumerate(image_urls, 1):
        res = _post(f"{user_id}/threads", {
            "media_type": "IMAGE",
            "image_url": url,
            "is_carousel_item": "true",
        }, token)
        cid = res.get("id")
        if not cid:
            raise ThreadsError(f"Threads child container 생성 실패 ({i}): {res}")
        children.append(cid)
        logger.info(f"  Threads child {i}/{len(image_urls)} id={cid}")
        if progress_cb:
            progress_cb("uploading", {"current": i, "total": total})

    # 2) 각 child FINISHED 대기 + 캐러셀 컨테이너 생성/대기
    if progress_cb:
        progress_cb("finalizing", {})
    for cid in children:
        _wait_for_container(cid, token)

    res = _post(f"{user_id}/threads", {
        "media_type": "CAROUSEL",
        "children": ",".join(children),
        "text": caption,
    }, token)
    carousel_id = res.get("id")
    if not carousel_id:
        raise ThreadsError(f"Threads carousel container 생성 실패: {res}")
    _wait_for_container(carousel_id, token)

    # 3) 게시
    if progress_cb:
        progress_cb("publishing", {})
    pub = _post(f"{user_id}/threads_publish", {
        "creation_id": carousel_id,
    }, token)
    media_id = pub.get("id")
    if not media_id:
        raise ThreadsError(f"Threads publish 응답에 id 없음: {pub}")
    logger.info(f"  Threads published id={media_id}")

    # permalink 조회
    permalink = None
    try:
        info = _get(media_id, {"fields": "permalink"}, token)
        permalink = info.get("permalink")
    except Exception:
        pass

    return {"media_id": media_id, "permalink": permalink}


def refresh_long_lived_token() -> str | None:
    """60일 long-lived 토큰을 사용 시점에 갱신 (Threads 권장)."""
    token = os.getenv("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        return None
    try:
        r = requests.get(
            "https://graph.threads.net/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": token},
            timeout=15,
        )
        if r.ok:
            return r.json().get("access_token")
    except Exception:
        logger.exception("Threads refresh_long_lived_token failed")
    return None

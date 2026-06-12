"""Threads 캐러셀 게시.

API 기반: https://graph.threads.net/v1.0 (인스타·페이스북과 다른 base URL)

흐름:
  1. (사전) 각 이미지 URL 이 외부에서 https 200 으로 받아지는지 확인 (터널 문제 즉시 차단)
  2. 각 이미지마다 POST /{threads-user-id}/threads
     media_type=IMAGE, image_url=..., is_carousel_item=true → child container id
     - 미디어 다운로드 실패(subcode 2207052)/unknown(code 1) 등 transient 면 재시도
  3. POST /{threads-user-id}/threads (media_type=CAROUSEL, children=ids, text=caption)
  4. POST /{threads-user-id}/threads_publish (creation_id) → 게시 (transient 면 재시도)
  5. 폴링 — 컨테이너 status 가 FINISHED 될 때까지 (생성 직후의 일시 4xx 는 무시하고 계속)

쓰레드는 인스타보다 미디어 페처가 더 잘 삐끗한다(같은 URL 도 한 번 실패 후 재시도하면
성공하는 경우가 잦음). 그래서 인스타(instagram.py)와 동급의 재시도·관용 정책을 둔다.

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

_POLL_INTERVAL = 3
_POLL_MAX = 30            # 최대 ~90초 / container
_POLL_MAX_VIDEO = 120     # 영상 컨테이너 최대 ~6분
_CREATE_TRIES = 6         # child/carousel 생성: 1 + 5 재시도
_PUBLISH_TRIES = 5        # 게시: 1 + 4 재시도
_RETRY_WAIT = (5, 10, 20, 30, 45)  # transient 재시도 백오프(초)


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


def _is_transient(msg: str) -> bool:
    """쓰레드가 잠깐 삐끗했을 때(재시도하면 풀릴 가능성이 높은) 오류인지.

    - 2207052: '미디어 다운로드 실패' — URL 이 실제로 접근 가능하면 보통 일시적.
    - code 1/2, 'An unknown error', is_transient:true: 처리 지연/내부 일시 오류.
    """
    low = msg.lower()
    return (
        '"is_transient":true' in low or
        '2207052' in msg or
        '"code":1' in msg or
        '"code":2' in msg or
        'an unknown error' in low or
        'media id is not available' in low or
        'not enough time' in low
    )


def _post(path: str, data: dict, token: str) -> dict:
    data = {**data, "access_token": token}
    r = requests.post(f"{GRAPH}/{path.lstrip('/')}", data=data, timeout=30)
    if r.status_code >= 400:
        raise ThreadsError(f"Threads POST {path} HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def _post_with_retry(path: str, data: dict, token: str, what: str) -> dict:
    """transient 오류면 백오프 재시도. 영구 오류면 즉시 raise."""
    last_err = ""
    for attempt in range(_CREATE_TRIES):
        if attempt > 0:
            wait_s = _RETRY_WAIT[min(attempt - 1, len(_RETRY_WAIT) - 1)]
            logger.info(f"Threads {what} 재시도 {attempt}/{_CREATE_TRIES - 1} (대기 {wait_s}s)")
            time.sleep(wait_s)
        try:
            return _post(path, data, token)
        except ThreadsError as e:
            last_err = str(e)
            if not _is_transient(last_err):
                raise
    raise ThreadsError(f"Threads {what} 실패(재시도 소진): {last_err}")


def _get(path: str, params: dict, token: str) -> dict:
    params = {**params, "access_token": token}
    r = requests.get(f"{GRAPH}/{path.lstrip('/')}", params=params, timeout=30)
    if r.status_code >= 400:
        raise ThreadsError(f"Threads GET {path} HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def _wait_for_container(container_id: str, token: str) -> None:
    """컨테이너가 FINISHED 가 될 때까지 폴링.

    컨테이너 생성 직후의 일시 4xx(아직 노드가 안 떠서 거부) 는 마지막 에러로만
    보관하고 계속 시도. ERROR/EXPIRED 면 error_message 와 함께 즉시 실패.
    """
    last_http_err = ""
    for _ in range(_POLL_MAX):
        time.sleep(_POLL_INTERVAL)
        try:
            r = requests.get(
                f"{GRAPH}/{container_id}",
                params={"fields": "status,error_message", "access_token": token},
                timeout=15,
            )
        except requests.RequestException as e:
            last_http_err = f"network: {e}"
            continue
        if r.status_code >= 400:
            last_http_err = f"HTTP {r.status_code}: {r.text[:200]}"
            continue
        info = r.json() or {}
        status = info.get("status")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise ThreadsError(f"Threads container {container_id} status={status}: {info}")
        # IN_PROGRESS — 계속
    raise ThreadsError(
        f"Threads container {container_id} 폴링 타임아웃 (last: {last_http_err or 'IN_PROGRESS'})"
    )


def _verify_reachable(url: str) -> None:
    """이미지 URL 이 외부에서 https 200 으로 받아지는지 사전 확인.

    트라이클라우드플레어 퀵터널 URL 은 재시작마다 바뀌므로, 발행 시점에 URL 이
    죽어있으면 쓰레드는 'media download 실패(2207052)' 로 떨어진다. 발행 전에
    잡아서 명확히 안내한다.
    """
    try:
        r = requests.head(url, timeout=15, allow_redirects=True)
        if r.status_code == 405:  # HEAD 미지원 → GET 으로 재확인
            r = requests.get(url, timeout=15, stream=True)
    except requests.RequestException as e:
        raise ThreadsError(f"이미지 URL 외부 접근 실패({e}). SERVER_URL/터널 상태 확인: {url}")
    if r.status_code != 200:
        raise ThreadsError(
            f"이미지 URL 이 외부에서 200 이 아님(HTTP {r.status_code}). "
            f"터널 URL 이 바뀌었는지(SERVER_URL) 확인: {url}"
        )
    ctype = (r.headers.get("content-type") or "").lower()
    if "image" not in ctype:
        raise ThreadsError(f"이미지 URL 의 content-type 이 이미지가 아님({ctype}): {url}")


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

    # 0) 사전 점검 — 모든 URL 이 외부에서 받아지는지 (터널 문제를 발행 전에 차단)
    for url in image_urls:
        _verify_reachable(url)

    # 1) 각 이미지를 child 컨테이너로 (transient 면 재시도)
    children: list[str] = []
    for i, url in enumerate(image_urls, 1):
        res = _post_with_retry(f"{user_id}/threads", {
            "media_type": "IMAGE",
            "image_url": url,
            "is_carousel_item": "true",
        }, token, what=f"child container({i})")
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

    res = _post_with_retry(f"{user_id}/threads", {
        "media_type": "CAROUSEL",
        "children": ",".join(children),
        "text": caption,
    }, token, what="carousel container")
    carousel_id = res.get("id")
    if not carousel_id:
        raise ThreadsError(f"Threads carousel container 생성 실패: {res}")
    _wait_for_container(carousel_id, token)

    # 3) 게시 — transient(컨테이너 처리 중/내부 일시 오류) 면 재시도
    if progress_cb:
        progress_cb("publishing", {})
    media_id = None
    last_err = ""
    for attempt in range(_PUBLISH_TRIES):
        if attempt > 0:
            wait_s = _RETRY_WAIT[min(attempt - 1, len(_RETRY_WAIT) - 1)]
            logger.info(f"Threads publish 재시도 {attempt}/{_PUBLISH_TRIES - 1} (대기 {wait_s}s)")
            time.sleep(wait_s)
        try:
            pub = _post(f"{user_id}/threads_publish", {
                "creation_id": carousel_id,
            }, token)
            media_id = pub.get("id")
            if media_id:
                break
            last_err = f"응답에 id 없음: {pub}"
        except ThreadsError as e:
            last_err = str(e)
            if not _is_transient(last_err):
                raise
    if not media_id:
        raise ThreadsError(f"Threads publish 실패(재시도 소진): {last_err}")
    logger.info(f"  Threads published id={media_id}")

    # permalink 조회
    permalink = None
    try:
        info = _get(media_id, {"fields": "permalink"}, token)
        permalink = info.get("permalink")
    except Exception:
        pass

    return {"media_id": media_id, "permalink": permalink}


def publish_mixed_carousel(items: list[dict], caption: str = "",
                           progress_cb=None) -> dict:
    """Threads 혼합 미디어(이미지+영상) 캐러셀 게시.

    items: [{"url": str, "media_type": "IMAGE" | "VIDEO"}, ...]  최대 20개.
           IMAGE-only 이면 publish_carousel 에 위임.
    progress_cb(phase, info): 단계 콜백.
    return: {"media_id": ..., "permalink": ...}
    """
    user_id, token = _env()
    if not items:
        raise ThreadsError("미디어가 없습니다")
    if len(items) > 20:
        items = items[:20]

    if all(it.get("media_type", "IMAGE").upper() == "IMAGE" for it in items):
        return publish_carousel([it["url"] for it in items], caption, progress_cb)

    total = len(items)
    if progress_cb:
        progress_cb("uploading", {"current": 0, "total": total})

    logger.info(f"Threads mixed carousel start: {len(items)} items")

    # 0) 사전 접근 가능 여부 확인 (이미지만 — 영상은 서버 처리 필요)
    for item in items:
        if item.get("media_type", "IMAGE").upper() == "IMAGE":
            _verify_reachable(item["url"])

    children: list[str] = []
    for i, item in enumerate(items, 1):
        mtype = item.get("media_type", "IMAGE").upper()
        if mtype == "VIDEO":
            data = {
                "video_url": item["url"],
                "media_type": "VIDEO",
                "is_carousel_item": "true",
            }
        else:
            data = {
                "image_url": item["url"],
                "media_type": "IMAGE",
                "is_carousel_item": "true",
            }
        res = _post_with_retry(f"{user_id}/threads", data, token, what=f"mixed child({i})")
        cid = res.get("id")
        if not cid:
            raise ThreadsError(f"Threads mixed child 생성 실패 ({i}): {res}")
        children.append(cid)
        logger.info(f"  Threads mixed child {i}/{len(items)} id={cid} type={mtype}")
        if progress_cb:
            progress_cb("uploading", {"current": i, "total": total})

    if progress_cb:
        progress_cb("finalizing", {})
    for cid in children:
        _wait_for_container(cid, token)

    res = _post_with_retry(f"{user_id}/threads", {
        "media_type": "CAROUSEL",
        "children": ",".join(children),
        "text": _truncate_for_threads(caption or ""),
    }, token, what="mixed carousel container")
    carousel_id = res.get("id")
    if not carousel_id:
        raise ThreadsError(f"Threads mixed carousel 생성 실패: {res}")
    _wait_for_container(carousel_id, token)

    if progress_cb:
        progress_cb("publishing", {})
    media_id = None
    last_err = ""
    for attempt in range(_PUBLISH_TRIES):
        if attempt > 0:
            wait_s = _RETRY_WAIT[min(attempt - 1, len(_RETRY_WAIT) - 1)]
            time.sleep(wait_s)
        try:
            pub = _post(f"{user_id}/threads_publish", {"creation_id": carousel_id}, token)
            media_id = pub.get("id")
            if media_id:
                break
            last_err = f"응답에 id 없음: {pub}"
        except ThreadsError as e:
            last_err = str(e)
            if not _is_transient(last_err):
                raise
    if not media_id:
        raise ThreadsError(f"Threads mixed publish 실패: {last_err}")

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

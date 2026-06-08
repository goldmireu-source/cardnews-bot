"""Instagram Graph API 캐러셀 게시.

기존 server.py 의 인라인 로직을 모듈로 추출.

ENV:
  - IG_USER_ID
  - IG_ACCESS_TOKEN
"""
import logging
import os
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v21.0"

# 컨테이너 status_code 폴링 설정
# child: 보통 10~30s 내 FINISHED. carousel: ~5s.
# 컨테이너 생성 직후 즉시 GET 호출은 종종 GraphMethodException(subcode 33)으로 거부됨
# (transient — 잠시 후 정상). 따라서 4xx 도 처음엔 무시하고 계속 시도.
_POLL_INTERVAL = 3
_POLL_MAX_CHILD = 30      # 최대 ~90초 / child
_POLL_MAX_CAROUSEL = 20   # 최대 ~60초 / carousel
_POLL_MAX_VIDEO = 120     # 영상 컨테이너 최대 ~6분 (인코딩 시간)
_PUBLISH_RETRY_MAX = 3
_PUBLISH_RETRY_WAIT = (5, 10, 15)


class InstagramError(RuntimeError):
    pass


def _wait_for_container(container_id: str, token: str, max_iter: int) -> None:
    """status_code=FINISHED 까지 폴링.

    transient 4xx (예: subcode 33 'Unsupported get request') 는 컨테이너가 막
    생성된 직후 잠깐 발생하므로 마지막 에러로만 보관하고 계속 시도. 끝까지 못
    풀리면 timeout 에러에 그 마지막 에러를 첨부해서 raise.
    """
    last_http_err = ""
    for _ in range(max_iter):
        time.sleep(_POLL_INTERVAL)
        try:
            r = requests.get(
                f"{GRAPH}/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
                timeout=15,
            )
        except requests.RequestException as e:
            last_http_err = f"network: {e}"
            continue
        if r.status_code >= 400:
            last_http_err = f"HTTP {r.status_code}: {r.text[:200]}"
            continue
        body = r.json() or {}
        sc = body.get("status_code")
        if sc == "FINISHED":
            return
        if sc in ("ERROR", "EXPIRED"):
            raise InstagramError(
                f"IG container {container_id} status={sc} ({body.get('status','')})"
            )
        # IN_PROGRESS — 계속
    raise InstagramError(
        f"IG container {container_id} 폴링 타임아웃 (last: {last_http_err or 'IN_PROGRESS'})"
    )


def _is_transient_publish_error(msg: str) -> bool:
    return (
        '"code":1' in msg or '"code":2' in msg or
        "Media ID is not available" in msg or
        "Media Builder failed" in msg or
        "An unknown error" in msg
    )


def _is_rate_limit_error(msg: str) -> bool:
    """'Application request limit reached'(code 4 / subcode 2207051) 류.

    인스타가 이 응답을 주면서도 실제 게시는 성공시키는 경우가 잦다. 재시도하면
    같은 제한에 걸리거나 중복 게시가 될 수 있으므로, 재시도 대신 '실제 게시 여부'를
    확인하는 경로로 보낸다.
    """
    low = msg.lower()
    return (
        '"code":4' in msg or '2207051' in msg or
        "application request limit reached" in low or
        "request limit reached" in low
    )


def _find_recent_media(user_id: str, token: str, since_epoch: float,
                       slack: float = 150.0) -> dict | None:
    """publish 시작 시각 이후에 올라온 최신 게시물을 찾아 반환(없으면 None).

    media_publish 가 제한 응답을 냈지만 게시가 실제로 됐는지 검증하는 용도.
    """
    try:
        r = requests.get(
            f"{GRAPH}/{user_id}/media",
            params={"fields": "id,permalink,timestamp", "limit": 3, "access_token": token},
            timeout=15,
        )
        if not r.ok:
            return None
        for m in (r.json().get("data") or []):  # 최신순
            ts = m.get("timestamp")
            if not ts:
                continue
            try:
                dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").timestamp()
            except ValueError:
                continue
            if dt >= since_epoch - slack:
                return m
        return None
    except Exception:
        return None


def _env() -> tuple[str, str]:
    user_id = os.getenv("IG_USER_ID", "").strip()
    token = os.getenv("IG_ACCESS_TOKEN", "").strip()
    if not user_id or not token:
        raise InstagramError(
            "IG_USER_ID / IG_ACCESS_TOKEN 미설정 (docs/POSTING_SETUP.md 참고)"
        )
    return user_id, token


def is_configured() -> bool:
    return bool(os.getenv("IG_USER_ID", "").strip()
                and os.getenv("IG_ACCESS_TOKEN", "").strip())


def _post(path: str, data: dict, token: str) -> dict:
    data = {**data, "access_token": token}
    r = requests.post(f"{GRAPH}/{path.lstrip('/')}", data=data, timeout=30)
    if r.status_code >= 400:
        raise InstagramError(f"IG POST {path} HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def publish_carousel(image_urls: list[str], caption: str = "",
                     progress_cb=None) -> dict:
    """Instagram 캐러셀 게시.

    image_urls: 외부에서 https 로 접근 가능한 절대 URL (최대 10장).
    progress_cb(phase, info): 단계 콜백. phase ∈ uploading|finalizing|publishing.
    return: {"media_id": ..., "permalink": ...}
    """
    user_id, token = _env()
    if not image_urls:
        raise InstagramError("이미지가 없습니다")
    if len(image_urls) > 10:
        image_urls = image_urls[:10]

    logger.info(f"IG carousel publish start: {len(image_urls)} images")
    total = len(image_urls)
    if progress_cb:
        progress_cb("uploading", {"current": 0, "total": total})

    # 1) child container 생성
    children: list[str] = []
    for i, url in enumerate(image_urls, 1):
        res = _post(f"{user_id}/media", {
            "image_url": url,
            "is_carousel_item": "true",
        }, token)
        cid = res.get("id")
        if not cid:
            raise InstagramError(f"IG child container 생성 실패 ({i}): {res}")
        children.append(cid)
        if progress_cb:
            progress_cb("uploading", {"current": i, "total": total})

    # 2) 모든 child FINISHED 대기 → carousel container 생성 → carousel FINISHED 대기
    if progress_cb:
        progress_cb("finalizing", {})
    for cid in children:
        _wait_for_container(cid, token, _POLL_MAX_CHILD)

    res = _post(f"{user_id}/media", {
        "media_type": "CAROUSEL",
        "children": ",".join(children),
        "caption": caption or "",
    }, token)
    carousel_id = res.get("id")
    if not carousel_id:
        raise InstagramError(f"IG carousel 생성 실패: {res}")
    _wait_for_container(carousel_id, token, _POLL_MAX_CAROUSEL)

    # 3) 게시 — transient 에러(컨테이너 처리 중) 면 재시도,
    #    rate-limit(code 4/2207051) 이면 재시도 대신 실제 게시 여부 확인으로 우회
    if progress_cb:
        progress_cb("publishing", {})
    media_id = None
    permalink = None
    last_err: str = ""
    publish_start = time.time()
    for attempt in range(_PUBLISH_RETRY_MAX):
        if attempt > 0:
            wait_s = _PUBLISH_RETRY_WAIT[min(attempt - 1, len(_PUBLISH_RETRY_WAIT) - 1)]
            logger.info(f"IG publish 재시도 {attempt}/{_PUBLISH_RETRY_MAX - 1} (대기 {wait_s}s)")
            time.sleep(wait_s)
        try:
            pub = _post(f"{user_id}/media_publish", {
                "creation_id": carousel_id,
            }, token)
            media_id = pub.get("id")
            if media_id:
                break
            last_err = f"응답에 id 없음: {pub}"
        except InstagramError as e:
            last_err = str(e)
            if _is_rate_limit_error(last_err):
                # 재시도하면 같은 제한·중복 게시 위험 → 실제 게시됐는지 확인으로
                logger.warning(f"IG media_publish 제한 응답 — 실제 게시 여부 확인: {last_err[:160]}")
                break
            if not _is_transient_publish_error(last_err):
                raise

    # 제한 응답 또는 재시도 소진 → 실제로 게시됐는지 최근 게시물로 확인
    if not media_id:
        recovered = _find_recent_media(user_id, token, publish_start)
        if recovered:
            media_id = recovered.get("id")
            permalink = recovered.get("permalink")
            logger.warning(
                f"IG media_publish 가 제한 응답을 냈지만 게시는 확인됨: media_id={media_id}"
            )
        else:
            raise InstagramError(f"IG publish 실패: {last_err}")

    # permalink 조회 (확인 경로에서 이미 얻었으면 생략)
    if permalink:
        return {"media_id": media_id, "permalink": permalink}
    try:
        r = requests.get(
            f"{GRAPH}/{media_id}",
            params={"fields": "permalink", "access_token": token},
            timeout=10,
        )
        if r.ok:
            permalink = r.json().get("permalink")
    except Exception:
        pass

    return {"media_id": media_id, "permalink": permalink}


def publish_mixed_carousel(items: list[dict], caption: str = "",
                           progress_cb=None) -> dict:
    """Instagram 혼합 미디어(이미지+영상) 캐러셀 게시.

    items: [{"url": str, "media_type": "IMAGE" | "VIDEO"}, ...]  최대 10개.
           IMAGE-only 목록이면 publish_carousel 에 위임.
    progress_cb(phase, info): 단계 콜백.
    return: {"media_id": ..., "permalink": ...}
    """
    user_id, token = _env()
    if not items:
        raise InstagramError("미디어가 없습니다")
    if len(items) > 10:
        items = items[:10]

    if all(it.get("media_type", "IMAGE").upper() == "IMAGE" for it in items):
        return publish_carousel([it["url"] for it in items], caption, progress_cb)

    total = len(items)
    if progress_cb:
        progress_cb("uploading", {"current": 0, "total": total})

    logger.info(f"IG mixed carousel start: {len(items)} items")

    children: list[dict] = []
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
                "is_carousel_item": "true",
            }
        res = _post(f"{user_id}/media", data, token)
        cid = res.get("id")
        if not cid:
            raise InstagramError(f"IG child container 생성 실패 ({i}): {res}")
        children.append({"id": cid, "media_type": mtype})
        logger.info(f"  IG mixed child {i}/{len(items)} id={cid} type={mtype}")
        if progress_cb:
            progress_cb("uploading", {"current": i, "total": total})

    if progress_cb:
        progress_cb("finalizing", {})
    for child in children:
        max_iter = _POLL_MAX_VIDEO if child["media_type"] == "VIDEO" else _POLL_MAX_CHILD
        _wait_for_container(child["id"], token, max_iter)

    res = _post(f"{user_id}/media", {
        "media_type": "CAROUSEL",
        "children": ",".join(c["id"] for c in children),
        "caption": caption or "",
    }, token)
    carousel_id = res.get("id")
    if not carousel_id:
        raise InstagramError(f"IG carousel 생성 실패: {res}")
    _wait_for_container(carousel_id, token, _POLL_MAX_CAROUSEL)

    if progress_cb:
        progress_cb("publishing", {})
    media_id = None
    permalink = None
    last_err: str = ""
    publish_start = time.time()
    for attempt in range(_PUBLISH_RETRY_MAX):
        if attempt > 0:
            wait_s = _PUBLISH_RETRY_WAIT[min(attempt - 1, len(_PUBLISH_RETRY_WAIT) - 1)]
            logger.info(f"IG mixed publish 재시도 {attempt} (대기 {wait_s}s)")
            time.sleep(wait_s)
        try:
            pub = _post(f"{user_id}/media_publish", {"creation_id": carousel_id}, token)
            media_id = pub.get("id")
            if media_id:
                break
            last_err = f"응답에 id 없음: {pub}"
        except InstagramError as e:
            last_err = str(e)
            if _is_rate_limit_error(last_err):
                break
            if not _is_transient_publish_error(last_err):
                raise

    if not media_id:
        recovered = _find_recent_media(user_id, token, publish_start)
        if recovered:
            media_id = recovered.get("id")
            permalink = recovered.get("permalink")
        else:
            raise InstagramError(f"IG mixed publish 실패: {last_err}")

    if permalink:
        return {"media_id": media_id, "permalink": permalink}
    try:
        r = requests.get(
            f"{GRAPH}/{media_id}",
            params={"fields": "permalink", "access_token": token},
            timeout=10,
        )
        if r.ok:
            permalink = r.json().get("permalink")
    except Exception:
        pass
    return {"media_id": media_id, "permalink": permalink}

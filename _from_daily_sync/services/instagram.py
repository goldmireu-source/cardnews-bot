"""Instagram Graph API 클라이언트 — 캐러셀 카드뉴스 게시.

캐러셀 게시 절차:
  1. 각 이미지마다 POST /{ig-user-id}/media (image_url, is_carousel_item=true) → child_container_id
  2. POST /{ig-user-id}/media (media_type=CAROUSEL, children=<csv ids>, caption) → carousel_container_id
  3. POST /{ig-user-id}/media_publish (creation_id=<carousel-container-id>) → 게시 완료

전제 조건:
  - Instagram 비즈니스/크리에이터 계정이 Facebook 페이지에 연결돼 있을 것
  - Meta 앱에 instagram_content_publish, pages_show_list 권한 승인 받음
  - long-lived page access token (Config.IG_ACCESS_TOKEN)
  - PUBLIC_BASE_URL — 슬라이드 PNG 가 외부에서 https 로 접근 가능해야 함
"""
import logging
import time
from typing import Iterable
from urllib.parse import urljoin

import requests

from config import Config

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# 컨테이너 status_code 폴링 인터벌
_POLL_INTERVAL_SEC = 2
_POLL_MAX_TRIES = 30


class InstagramError(RuntimeError):
    pass


def _check_env() -> None:
    missing = [k for k in ("IG_USER_ID", "IG_ACCESS_TOKEN", "PUBLIC_BASE_URL")
               if not getattr(Config, k)]
    if missing:
        raise InstagramError(f"미설정 ENV: {', '.join(missing)} — docs/instagram_setup.md 참고")


def _absolute_image_url(static_path: str) -> str:
    """/static/instagram/1/slide1.png 같은 상대 경로를 PUBLIC_BASE_URL 기준 절대 URL 로."""
    base = Config.PUBLIC_BASE_URL.rstrip("/") + "/"
    return urljoin(base, static_path.lstrip("/"))


def _post(path: str, data: dict) -> dict:
    url = f"{GRAPH_BASE}/{path.lstrip('/')}"
    data = {**data, "access_token": Config.IG_ACCESS_TOKEN}
    r = requests.post(url, data=data, timeout=30)
    if r.status_code >= 400:
        raise InstagramError(f"POST {path} HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{GRAPH_BASE}/{path.lstrip('/')}"
    params = {**(params or {}), "access_token": Config.IG_ACCESS_TOKEN}
    r = requests.get(url, params=params, timeout=30)
    if r.status_code >= 400:
        raise InstagramError(f"GET {path} HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def _wait_for_container(container_id: str) -> None:
    """컨테이너가 FINISHED 상태가 될 때까지 폴링.

    인스타가 image_url 을 다운로드/검증하는 데 몇 초 걸리는 경우가 있음.
    IN_PROGRESS 면 대기. ERROR/EXPIRED 면 예외.
    """
    for attempt in range(_POLL_MAX_TRIES):
        time.sleep(_POLL_INTERVAL_SEC)
        info = _get(container_id, params={"fields": "status_code,status"})
        code = info.get("status_code") or info.get("status")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError(f"container {container_id} status={code} info={info}")
        logger.debug(f"container {container_id} status={code} (try {attempt+1})")
    raise InstagramError(f"container {container_id} did not finish in time")


def _create_child_container(image_url: str) -> str:
    res = _post(f"{Config.IG_USER_ID}/media", {
        "image_url": image_url,
        "is_carousel_item": "true",
    })
    cid = res.get("id")
    if not cid:
        raise InstagramError(f"no container id in response: {res}")
    return cid


def _create_carousel_container(children_ids: list[str], caption: str) -> str:
    res = _post(f"{Config.IG_USER_ID}/media", {
        "media_type": "CAROUSEL",
        "children": ",".join(children_ids),
        "caption": caption,
    })
    cid = res.get("id")
    if not cid:
        raise InstagramError(f"no carousel container id in response: {res}")
    return cid


def _publish(container_id: str) -> dict:
    res = _post(f"{Config.IG_USER_ID}/media_publish", {
        "creation_id": container_id,
    })
    if "id" not in res:
        raise InstagramError(f"publish did not return media id: {res}")
    return res


def _get_permalink(media_id: str) -> str | None:
    try:
        info = _get(media_id, params={"fields": "permalink"})
        return info.get("permalink")
    except Exception:
        return None


def publish_carousel(image_static_paths: list[str], caption: str) -> dict:
    """캐러셀 게시 — 메인 엔트리.

    image_static_paths: ["/static/instagram/1/slide1.png", ...] (Flask static 상대 경로)
    caption: 인스타 캡션 (해시태그 포함 전체 텍스트)
    return: {"media_id": ..., "permalink": ...}
    """
    _check_env()
    if not image_static_paths:
        raise InstagramError("이미지가 없습니다")
    if len(image_static_paths) > 10:
        raise InstagramError("캐러셀은 최대 10장까지 가능합니다 (인스타 제한)")

    urls = [_absolute_image_url(p) for p in image_static_paths]
    logger.info(f"carousel publish start: {len(urls)} images")

    # 1. 각 슬라이드 child 컨테이너 생성
    children = []
    for i, url in enumerate(urls, 1):
        cid = _create_child_container(url)
        children.append(cid)
        logger.info(f"  child {i}/{len(urls)} container={cid}")

    # 2. 각 child 가 FINISHED 가 될 때까지 대기 (인스타가 이미지 다운로드/검증)
    for cid in children:
        _wait_for_container(cid)

    # 3. 캐러셀 컨테이너 생성
    carousel = _create_carousel_container(children, caption)
    logger.info(f"  carousel container={carousel}")
    _wait_for_container(carousel)

    # 4. 게시
    pub = _publish(carousel)
    media_id = pub["id"]
    permalink = _get_permalink(media_id)
    logger.info(f"  published media={media_id} permalink={permalink}")

    return {"media_id": media_id, "permalink": permalink}


def refresh_long_lived_token() -> str | None:
    """페이지 long-lived 토큰 재발급 (60일 → 60일).

    IG_FB_PAGE_ID + 기존 토큰으로 새 토큰 받기. .env 에 직접 갱신 못 함 →
    반환값을 로그/UI 에 노출해서 사용자가 수동 갱신.
    """
    if not (Config.IG_ACCESS_TOKEN and Config.IG_FB_PAGE_ID):
        return None
    try:
        info = _get(Config.IG_FB_PAGE_ID, params={"fields": "access_token"})
        return info.get("access_token")
    except Exception:
        logger.exception("refresh_long_lived_token failed")
        return None

"""장기 토큰 자동 갱신.

대상:
  - Threads long-lived user token (60일, refresh 호출 시 새 60일로 연장)
  - Meta(IG/FB) page access token (페이지 소유 유지 시 사실상 무만료지만,
    fb_exchange_token 으로 재발급하여 새 토큰으로 교체)

흐름:
  1) 각 플랫폼 그래프 API 호출하여 새 토큰 수령
  2) 성공 시 .env 파일의 해당 라인만 원자적 교체 + os.environ 동시 갱신
  3) 실패는 로깅만 — 기존 토큰 유지 (.env 손상 방지)

ENV (입력):
  THREADS_ACCESS_TOKEN
  FB_PAGE_ACCESS_TOKEN, FB_APP_ID, FB_APP_SECRET
  IG_ACCESS_TOKEN   (FB_PAGE_ACCESS_TOKEN 과 동일 값 — 같이 갱신)
"""
import logging
import os
import tempfile
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

THREADS_GRAPH = "https://graph.threads.net"
META_GRAPH = "https://graph.facebook.com/v21.0"


class TokenRefreshError(RuntimeError):
    pass


def update_env_var(key: str, value: str, env_path: Path = ENV_PATH) -> None:
    """`.env` 의 KEY=... 라인을 원자적으로 교체. 키가 없으면 끝에 추가.

    원자성: 임시 파일에 새 내용 쓴 뒤 os.replace 로 교환 → 중간 크래시에도 손상 없음.
    """
    if not env_path.exists():
        raise TokenRefreshError(f".env 없음: {env_path}")

    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    replaced = False
    new_line = f"{key}={value}\n"
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(new_line)

    fd, tmp_path = tempfile.mkstemp(prefix=".env.", dir=str(env_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp_path, env_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    os.environ[key] = value


def refresh_threads_token() -> dict:
    """Threads long-lived 토큰을 새 60일로 갱신.

    주의: 토큰 발급 후 24시간이 지나야 호출 가능. 첫 호출은 실패할 수 있음.
    """
    token = os.getenv("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        return {"platform": "threads", "ok": False, "reason": "no_token"}

    try:
        r = requests.get(
            f"{THREADS_GRAPH}/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": token},
            timeout=20,
        )
    except Exception as e:
        logger.exception("threads refresh request failed")
        return {"platform": "threads", "ok": False, "reason": f"request: {e}"}

    if not r.ok:
        logger.warning(f"threads refresh HTTP {r.status_code}: {r.text[:300]}")
        return {"platform": "threads", "ok": False, "reason": f"http_{r.status_code}", "body": r.text[:300]}

    new_token = (r.json() or {}).get("access_token")
    if not new_token:
        return {"platform": "threads", "ok": False, "reason": "no_access_token_in_response"}

    update_env_var("THREADS_ACCESS_TOKEN", new_token)
    expires_in = (r.json() or {}).get("expires_in")
    logger.info(f"threads token refreshed (expires_in={expires_in})")
    return {"platform": "threads", "ok": True, "expires_in": expires_in}


def refresh_meta_page_token() -> dict:
    """FB page access token 을 fb_exchange_token 으로 재발급.

    동일 토큰이 `IG_ACCESS_TOKEN` 으로도 쓰이므로 두 키 모두 새 값으로 교체.
    """
    page_token = os.getenv("FB_PAGE_ACCESS_TOKEN", "").strip()
    app_id = os.getenv("FB_APP_ID", "").strip()
    app_secret = os.getenv("FB_APP_SECRET", "").strip()
    if not (page_token and app_id and app_secret):
        return {"platform": "meta", "ok": False, "reason": "missing_env"}

    try:
        r = requests.get(
            f"{META_GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": page_token,
            },
            timeout=20,
        )
    except Exception as e:
        logger.exception("meta refresh request failed")
        return {"platform": "meta", "ok": False, "reason": f"request: {e}"}

    if not r.ok:
        logger.warning(f"meta refresh HTTP {r.status_code}: {r.text[:300]}")
        return {"platform": "meta", "ok": False, "reason": f"http_{r.status_code}", "body": r.text[:300]}

    new_token = (r.json() or {}).get("access_token")
    if not new_token:
        return {"platform": "meta", "ok": False, "reason": "no_access_token_in_response"}

    update_env_var("FB_PAGE_ACCESS_TOKEN", new_token)
    if os.getenv("IG_ACCESS_TOKEN", "").strip():
        update_env_var("IG_ACCESS_TOKEN", new_token)
    expires_in = (r.json() or {}).get("expires_in")
    logger.info(f"meta page token refreshed (expires_in={expires_in})")
    return {"platform": "meta", "ok": True, "expires_in": expires_in}


def refresh_tiktok_token() -> dict:
    """TikTok access_token + refresh_token 둘 다 .env 에 갱신.

    응답에 새 refresh_token 도 함께 옴 — 그 새 값으로 교체해야 다음 갱신이 동작.
    설정 미비 시 silent skip (TikTok 미사용 사용자 케이스).
    """
    from services import tiktok as tk_svc
    if not (os.getenv("TIKTOK_CLIENT_KEY", "").strip() and
            os.getenv("TIKTOK_CLIENT_SECRET", "").strip() and
            os.getenv("TIKTOK_REFRESH_TOKEN", "").strip()):
        return {"platform": "tiktok", "ok": False, "skipped": True, "reason": "미설정"}

    body = tk_svc.refresh_token()
    if not body:
        return {"platform": "tiktok", "ok": False, "reason": "refresh 실패"}

    new_access = body.get("access_token")
    new_refresh = body.get("refresh_token")
    expires_in = body.get("expires_in")
    if not new_access:
        return {"platform": "tiktok", "ok": False, "reason": "응답에 access_token 없음"}

    update_env_var("TIKTOK_ACCESS_TOKEN", new_access)
    if new_refresh:
        update_env_var("TIKTOK_REFRESH_TOKEN", new_refresh)
    logger.info(f"tiktok token refreshed (expires_in={expires_in})")
    return {"platform": "tiktok", "ok": True, "expires_in": expires_in}


def refresh_all() -> dict:
    """Threads + Meta + TikTok 갱신 시도. 한쪽 실패가 다른쪽 막지 않음."""
    results = []
    for fn in (refresh_threads_token, refresh_meta_page_token, refresh_tiktok_token):
        try:
            results.append(fn())
        except Exception as e:
            logger.exception(f"{fn.__name__} crashed")
            results.append({"platform": fn.__name__, "ok": False, "reason": f"crash: {e}"})
    # skipped 는 ok 판정에서 제외
    real = [r for r in results if not r.get("skipped")]
    summary = {"results": results, "all_ok": bool(real) and all(r.get("ok") for r in real)}
    logger.info(f"token refresh summary: {summary}")
    return summary

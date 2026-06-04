"""헤드리스 브라우저로 세션 PNG 자동 렌더 + 인스타 발행.

기존 흐름(`/?session=X&publish=1` 으로 스튜디오 열면 html2canvas 가
모든 카드를 렌더해서 /api/uploads/<id> 로 자동 업로드)을 헤드리스 chromium 으로 재현.

요구사항:
  - pip install playwright; python -m playwright install chromium
  - server.py 가 LOCAL 에서 가동 중이어야 함 (자기 자신에게 요청)
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

LOCAL_BASE = os.getenv("LOCAL_BASE_URL", f"http://localhost:{os.getenv('PORT', '5050')}")

# html2canvas 렌더 타임아웃 (카드 최대 15장 × ~1초 + 여유)
RENDER_TIMEOUT_MS = 120_000


def render_session_to_pngs(session_id: str) -> int:
    """헤드리스 chromium 으로 /?session=X&publish=1 를 열고 PNG 업로드 완료까지 대기.

    반환: 업로드된 PNG 개수 (실패 시 예외).
    """
    from playwright.sync_api import sync_playwright

    url = f"{LOCAL_BASE}/?session={session_id}&publish=1"
    logger.info(f"headless render start: {url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 1500},
                # html2canvas 가 외부 폰트/이미지를 잘 fetch 하도록
                ignore_https_errors=True,
            )
            page = ctx.new_page()

            uploaded_count = {"n": 0}

            def _on_response(resp):
                # /api/uploads/<id> POST 응답 — 업로드 완료 시그널
                if resp.url.endswith(f"/api/uploads/{session_id}") and resp.request.method == "POST":
                    try:
                        body = resp.json()
                        uploaded_count["n"] = len(body.get("files", []))
                        logger.info(f"  upload response: {uploaded_count['n']} files")
                    except Exception:
                        pass

            page.on("response", _on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)

            # 업로드 응답 기다리기 (Playwright 의 wait_for_event 활용)
            try:
                page.wait_for_response(
                    lambda r: r.url.endswith(f"/api/uploads/{session_id}")
                              and r.request.method == "POST"
                              and r.status < 400,
                    timeout=RENDER_TIMEOUT_MS,
                )
            except Exception as e:
                raise RuntimeError(f"PNG 업로드 응답 대기 실패: {e}")

            # 약간의 추가 안정화 시간
            time.sleep(0.5)
            return uploaded_count["n"] or 0
        finally:
            browser.close()


def publish_session_to_instagram(session_id: str, caption: str = "") -> str:
    """이미 PNG 업로드된 세션을 IG Graph API 로 발행.

    내부적으로 /api/instagram/publish/<session_id> 호출. media_id 반환.
    """
    url = f"{LOCAL_BASE}/api/instagram/publish/{session_id}"
    payload = {"caption": caption} if caption else {}
    r = requests.post(url, json=payload, timeout=180)
    if r.status_code >= 400:
        try:
            err = r.json().get("error") or r.text
        except Exception:
            err = r.text
        raise RuntimeError(f"IG publish 실패: HTTP {r.status_code} — {err}")
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"IG publish 실패: {body}")
    return body.get("media_id", "")


def render_and_publish(session_id: str, caption: str = "") -> str:
    """원샷: 헤드리스 렌더 → 업로드 → IG 발행. media_id 반환."""
    n = render_session_to_pngs(session_id)
    if n == 0:
        raise RuntimeError("렌더링된 PNG 가 0개입니다")
    logger.info(f"  {n} PNGs uploaded, now publishing to IG")
    return publish_session_to_instagram(session_id, caption)


if __name__ == "__main__":
    # 수동 테스트: python headless_publish.py auto_news_1234567890
    import sys
    if len(sys.argv) < 2:
        print("usage: python headless_publish.py <session_id>")
        sys.exit(1)
    logging.basicConfig(level=logging.INFO)
    print(render_and_publish(sys.argv[1]))

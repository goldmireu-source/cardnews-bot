"""클라우드 도메인(발행용 공개 HTTPS URL) 자동 새로고침.

trycloudflare 퀵터널은 띄울 때마다 랜덤 도메인이라, 끊기면 다시 받아야 한다.
이 스크립트 한 번이면:
  1) 떠 있는 옛 cloudflared 정리
  2) 새 퀵터널 기동(백그라운드 유지 — 끄면 안 됨)
  3) 새 https://<랜덤>.trycloudflare.com URL 추출
  4) .env 의 SERVER_URL 을 새 URL 로 갱신 (다른 값은 보존)
  5) 서버 재시작 안내

사용:
  python refresh_tunnel.py          # 기본 포트(PORT env 또는 5050)
  python refresh_tunnel.py 5050     # 포트 직접 지정
"""
import os
import re
import sys
import time
import subprocess
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # cp949 콘솔에서 한글 출력 깨짐/크래시 방지
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "cloudflared.log"
ENV = ROOT / ".env"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_cloudflared() -> str:
    for name in ("cloudflared.exe.exe", "cloudflared.exe", "cloudflared"):
        p = ROOT / name
        if p.exists():
            return str(p)
    # PATH 폴백
    from shutil import which
    found = which("cloudflared")
    if found:
        return found
    print("[!] cloudflared 실행파일을 찾지 못했습니다 (프로젝트 폴더 또는 PATH).")
    sys.exit(1)


def kill_old():
    for img in ("cloudflared.exe.exe", "cloudflared.exe"):
        try:
            # 출력은 시스템 로캘(cp949)이라 디코딩하지 않고 버린다
            subprocess.run(["taskkill", "/F", "/IM", img],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def start_tunnel(cf: str, port: str) -> subprocess.Popen:
    logf = open(LOG, "w", encoding="utf-8", errors="ignore")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        [cf, "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
        stdout=logf, stderr=subprocess.STDOUT, cwd=str(ROOT),
        creationflags=flags,
    )


def wait_for_url(timeout: float = 40.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            text = LOG.read_text(encoding="utf-8", errors="ignore")
        except FileNotFoundError:
            continue
        m = URL_RE.search(text)
        if m:
            return m.group(0)
    return None


def update_env(key: str, value: str):
    lines, found = [], False
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if re.match(rf"\s*{re.escape(key)}\s*=", line):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else os.getenv("PORT", "5050")
    cf = find_cloudflared()
    print(f"[*] cloudflared: {cf}")
    print(f"[*] 대상 포트: {port}")

    print("[*] 옛 cloudflared 정리…")
    kill_old()
    time.sleep(1.5)

    print("[*] 새 퀵터널 기동…")
    proc = start_tunnel(cf, port)

    print("[*] 새 URL 대기(최대 40초)…")
    url = wait_for_url()
    if not url:
        print("[!] URL 추출 실패. cloudflared.log 를 확인하세요.")
        sys.exit(2)

    update_env("SERVER_URL", url)
    print()
    print("=" * 60)
    print(f"  새 클라우드 도메인: {url}")
    print(f"  .env 의 SERVER_URL 갱신 완료")
    print(f"  cloudflared PID {proc.pid} 백그라운드 실행 중 (끄지 마세요)")
    print("=" * 60)
    print()
    print(">> 이제 서버를 재시작하면 새 도메인이 적용됩니다:")
    print("   (서버 터미널에서) Ctrl+C 후  python server.py")


if __name__ == "__main__":
    main()

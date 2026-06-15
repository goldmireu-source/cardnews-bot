# Cardnews Bot

> Telegram으로 텍스트·이미지를 보내면 Claude AI가 SNS 카드뉴스를 자동 생성하고 Instagram·Facebook·Threads에 예약 발행하는 자동화 파이프라인

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                         Telegram                                 │
│  텍스트 / 이미지(OCR) 전송  →  bot.py  →  Claude API            │
└─────────────────────────────┬────────────────────────────────────┘
                              │ 세션 저장 (JSON)
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Web Studio  (server.py)                       │
│  카드 편집 · 레이아웃 변경 · PNG 일괄 다운로드                  │
│  Claude API 프록시 · 이미지 검색 · 발행 큐 관리                 │
└─────────────────────────────┬────────────────────────────────────┘
                              │ APScheduler
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                   Auto Publisher  (auto_jobs.py)                 │
│  매일 07:30 KST 자동 생성 · 매 15분 예약 발행 실행              │
│  Instagram · Facebook · Threads · TikTok Graph API              │
└──────────────────────────────────────────────────────────────────┘
```

---

## Features

- **Telegram 인터페이스** — 텍스트 또는 사진(최대 5장)을 전송하면 5–7장 카드뉴스 자동 생성
- **OCR 지원** — 손글씨 메모, 강의 슬라이드, 화이트보드 사진에서 텍스트 추출 후 카드화
- **웹 편집 스튜디오** — 브라우저 기반 WYSIWYG 편집, 카드별 레이아웃·테마·텍스트 수정, PNG 일괄 다운로드
- **멀티 채널 발행** — Instagram, Facebook, Threads, TikTok Graph API 캐러셀 발행
- **예약 발행 & 자동화** — APScheduler로 매일 뉴스 1건 + 툴팁 1건 자동 생성, 발행 시각 예약
- **데일리싱크 연동** — 외부 뉴스 클러스터 DB에서 오늘의 주요 뉴스를 자동으로 카드화
- **톤 선택** — `[MZ]` `[감성]` `[진지]` `[핵심]` `[친근]` 태그로 글쓰기 스타일 전환
- **토큰 자동 갱신** — Facebook/Instagram/Threads 장기 액세스 토큰 자동 리프레시
- **코딩봇** (선택) — Telegram 채팅으로 서버 코드 수정·git 동기화 원격 실행

---

## Tech Stack

| 분류 | 기술 |
|------|------|
| Language | Python 3.10+ |
| Web Framework | Flask 3.x |
| AI | Anthropic Claude API (Sonnet / Haiku) |
| Telegram | python-telegram-bot 20.x |
| Scheduler | APScheduler 3.x |
| Headless Browser | Playwright |
| HTML Parsing | BeautifulSoup4 · lxml |
| SNS API | Instagram Graph API · Facebook Graph API · Threads API · TikTok API |
| Storage | SQLite (뉴스 DB) · JSON 파일 (세션) |
| Tunnel | Cloudflare Tunnel / ngrok |

---

## Project Structure

```
cardnews_bot/
├── server.py              # Flask 웹 서버 + 카드뉴스 스튜디오 + SNS 발행 API
├── bot.py                 # Telegram 봇 (수신 → Claude 생성 → 세션 저장)
├── auto_jobs.py           # APScheduler 자동화 잡 (일별 생성 + 예약 발행)
├── auto_scheduler.py      # 스케줄러 프로세스 진입점
├── article_images.py      # 기사 이미지 검색 · 캐싱
├── code_agent_bot.py      # 원격 코딩봇 (선택)
├── headless_publish.py    # Playwright 헤드리스 발행 실행기
├── cardnews_studio.html   # 웹 편집 스튜디오 (single-page app)
├── auto_studio.html       # 자동화 대시보드 UI
├── data/
│   └── tooltip_topics.yaml  # 툴팁 주제 풀
├── sessions/              # 생성된 카드뉴스 세션 (git 제외)
├── uploads/               # 렌더링된 PNG (git 제외)
├── .env.example           # 환경변수 템플릿
└── requirements.txt
```

---

## Quick Start

### 1. 환경 설정

```bash
git clone https://github.com/your-username/cardnews-bot.git
cd cardnews-bot

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\Activate.ps1

pip install -r requirements.txt
playwright install chromium     # 헤드리스 발행용
```

### 2. 환경변수 설정

```bash
cp .env.example .env
```

`.env`에서 아래 항목을 채웁니다.

```dotenv
# 필수
TELEGRAM_BOT_TOKEN=           # @BotFather에서 발급
ANTHROPIC_API_KEY=            # console.anthropic.com
WEB_USERNAME=admin
WEB_PASSWORD=
SECRET_KEY=                   # secrets.token_hex(32)

# 선택 — SNS 발행 사용 시
IG_USER_ID=
IG_ACCESS_TOKEN=
FB_PAGE_ACCESS_TOKEN=
THREADS_ACCESS_TOKEN=
SERVER_URL=https://your-domain.com   # 외부 접근 가능한 HTTPS URL (Instagram 필수)
```

### 3. 실행

```bash
# 터미널 1 — 웹 서버 + 스튜디오
python server.py

# 터미널 2 — Telegram 봇
python bot.py

# 터미널 3 (선택) — 자동화 스케줄러
python auto_scheduler.py
```

브라우저에서 `http://localhost:5050` 접속 → 스튜디오 확인.

### 4. 외부 URL 노출 (SNS 발행 필수)

Instagram Graph API는 공개 HTTPS URL을 요구합니다.

```bash
# Cloudflare Tunnel (무료, 권장)
cloudflared tunnel --url http://localhost:5050

# 또는 ngrok
ngrok http 5050
```

발급된 URL을 `.env`의 `SERVER_URL`에 설정 후 서버 재시작.

---

## Environment Variables

| 변수 | 필수 | 설명 |
| ---- | ---- | ---- |
| `TELEGRAM_BOT_TOKEN` | ✅ | Telegram 봇 토큰 |
| `ANTHROPIC_API_KEY` | ✅ | Claude API 키 |
| `WEB_USERNAME` / `WEB_PASSWORD` | ✅ | 스튜디오 로그인 |
| `SECRET_KEY` | ✅ | Flask 세션 서명 키 |
| `SERVER_URL` | SNS 발행 시 ✅ | 외부 HTTPS 서버 주소 |
| `ALLOWED_USERS` | 권장 | 봇 허용 Telegram user ID (콤마 구분) |
| `IG_USER_ID` / `IG_ACCESS_TOKEN` | 선택 | Instagram 발행 |
| `FB_PAGE_ACCESS_TOKEN` | 선택 | Facebook 발행 |
| `THREADS_ACCESS_TOKEN` | 선택 | Threads 발행 |
| `MODEL` | 선택 | Claude 모델 (기본: `claude-sonnet-4-20250514`) |
| `AUTO_PUBLISH` | 선택 | 자동 발행 on/off (기본: `true`) |

전체 목록은 [`.env.example`](.env.example) 참고.

---

## Usage

### Telegram 봇

| 입력 | 결과 |
|------|------|
| 텍스트 메시지 | 내용 분석 → 카드뉴스 5–7장 생성 |
| 사진 1–5장 | OCR + 이미지 분석 → 카드뉴스 생성 |
| `[MZ]` `[감성]` 등 태그 + 메시지 | 해당 톤으로 생성 |
| `/list` | 최근 생성 세션 목록 |
| `/last` | 마지막 세션 스튜디오 링크 |
| `/whoami` | 본인 Telegram user ID 확인 |

### 비용 참고

| 시나리오 | 모델 | 1회 |
| ------- | ---- | --- |
| 텍스트 | Sonnet 4 | ~$0.015 |
| 이미지 1장 | Sonnet 4 | ~$0.03 |
| 이미지 5장 | Sonnet 4 | ~$0.08 |
| 텍스트 | Haiku 4.5 | ~$0.002 |

---

## License

MIT

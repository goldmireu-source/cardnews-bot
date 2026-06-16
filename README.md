# Cardnews Bot

> Telegram으로 텍스트·이미지를 보내면 Claude AI가 SNS 카드뉴스를 자동 생성하고 Instagram·Facebook·Threads에 예약 발행하는 자동화 파이프라인
>
> ---
>
> ## Architecture
>
> ```
> ┌──────────────────────────────────────────────────────────────────┐
> │                          Telegram                                │
> │   텍스트 / 이미지(OCR) 전송  →  bot.py  →  Claude API           │
> └─────────────────────────────┬────────────────────────────────────┘
>                               │ 세션 저장 (JSON)
>                               ▼
> ┌──────────────────────────────────────────────────────────────────┐
> │                    Web Studio (server.py)                        │
> │   카드 편집 · 레이아웃 변경 · PNG 일괄 다운로드                  │
> │   Claude API 프록시 · 이미지 검색 · 발행 큐 관리                │
> └─────────────────────────────┬────────────────────────────────────┘
>                               │ APScheduler
>                               ▼
> ┌──────────────────────────────────────────────────────────────────┐
> │               Auto Publisher (auto_jobs.py)                     │
> │   매일 07:30 KST 자동 생성 · 매 15분 예약 발행 실행             │
> │   Instagram · Facebook · Threads · TikTok Graph API             │
> └──────────────────────────────────────────────────────────────────┘
> ```
>
> ---
>
> ## Features
>
> - **Telegram 인터페이스** — 텍스트 또는 사진(최대 5장)을 전송하면 5–7장 카드뉴스 자동 생성
> - - **OCR 지원** — 손글씨 메모, 강의 슬라이드, 화이트보드 사진에서 텍스트 추출 후 카드화
>   - - **웹 편집 스튜디오** — 브라우저 기반 WYSIWYG 편집, 카드별 레이아웃·테마·텍스트 수정, PNG 일괄 다운로드
>     - - **멀티 채널 발행** — Instagram, Facebook, Threads, TikTok Graph API 캐러셀 발행
>       - - **예약 발행 & 자동화** — APScheduler로 매일 뉴스 1건 + 툴팁 1건 자동 생성, 발행 시각 예약
>         - - **데일리싱크 연동** — 외부 뉴스 클러스터 DB에서 오늘의 주요 뉴스를 자동으로 카드화
>           - - **톤 선택** — `[MZ]` `[감성]` `[진지]` `[핵심]` `[친근]` 태그로 글쓰기 스타일 전환
>             - - **토큰 자동 갱신** — Facebook/Instagram/Threads 장기 액세스 토큰 자동 리프레시
>               - - **코딩봇 (선택)** — Telegram 채팅으로 서버 코드 수정·git 동기화 원격 실행
>                
>                 - ---
>
> ## Tech Stack
>
> | 분류 | 기술 | 선택 이유 |
> |------|------|-----------|
> | Language | Python 3.10+ | 비동기 I/O(`asyncio`), AI/ML 생태계, Telegram·SNS SDK가 모두 Python 기반이라 단일 언어로 전체 파이프라인을 구성할 수 있었음 |
> | Web Framework | Flask 3.x | FastAPI도 검토했으나, 단일 서버에서 웹 스튜디오 UI + API + 정적 파일을 동시에 서빙하는 단순한 구조에는 Flask의 낮은 설정 비용이 적합했음. 비동기 엔드포인트가 필요한 부분은 `asyncio.run()`으로 처리 |
> | AI | Anthropic Claude API (Sonnet / Haiku) | GPT-4o, Gemini 1.5도 비교 테스트. Claude는 **긴 한국어 텍스트를 카드 단위로 구조화**하는 능력과 시스템 프롬프트 준수율이 가장 높았음. 특히 "5~7장, 장당 3줄 이내" 같은 형식 제약을 일관되게 지켜 후처리 코드가 대폭 줄어듦 |
> | Telegram | python-telegram-bot 20.x | 공식 비동기 SDK. `Application` 기반의 핸들러 구조가 봇 로직을 모듈화하기 쉽고, 파일/사진 수신·전송 API가 직관적임 |
> | Scheduler | APScheduler 3.x | 별도 Celery + Redis 스택 없이 인프라 비용 0으로 cron 스타일 잡 스케줄링을 구현. 프로세스 내부에서 실행되므로 Flask 앱 컨텍스트 공유가 간단함 |
> | Headless Browser | Playwright | Selenium과 비교해 **비동기 네이티브** 지원과 Chromium 번들 설치(`playwright install chromium`)로 환경 구성이 단순함. HTML 카드뉴스 → PNG 변환 시 렌더링 정확도가 높음 |
> | HTML Parsing | BeautifulSoup4 · lxml | 뉴스 클러스터 DB에서 가져온 HTML 기사 본문 정제에 사용. lxml 파서가 속도·안정성 면에서 html.parser보다 우수함 |
> | SNS API | Instagram Graph API · Facebook Graph API · Threads API · TikTok API | 각 플랫폼의 공식 Graph API만 사용. 비공식 API는 계정 제재 리스크가 있어 처음부터 공식 경로를 선택 |
> | Storage | SQLite · JSON 파일 | 뉴스 메타데이터는 SQLite로 중복 방지·조회 최적화. 카드뉴스 세션은 JSON 파일로 관리해 DB 스키마 없이 카드 구조를 자유롭게 변경 가능 |
> | Tunnel | Cloudflare Tunnel / ngrok | Instagram Graph API가 공개 HTTPS URL을 필수로 요구함. Cloudflare Tunnel은 무료·고정 도메인·SSL 자동 발급으로 운영 편의성이 높아 기본 권장 옵션으로 채택 |
>
> ---
>
> ## AI 모델 선택 과정
>
> ### 왜 Claude인가
>
> 초기 프로토타입은 OpenAI `gpt-4o`로 시작했습니다. 카드뉴스라는 특성상 **출력 형식 일관성**이 매우 중요한데, 다음 두 가지 이유로 Claude로 전환했습니다.
>
> 1. **형식 준수율** — "5~7장, 장당 제목 1줄 + 본문 3줄 이내, JSON 배열 반환" 지시를 GPT-4o는 10회 중 2~3회 어겼지만(장 수 초과, 마크다운 감싸기 등), Claude Sonnet은 거의 항상 준수해 파싱 실패 예외 처리 코드가 크게 줄었습니다.
> 2. 2. **한국어 문체** — MZ·감성·진지 등 톤 지시에 Claude가 더 자연스러운 한국어 문체 변화를 보여줬습니다.
>   
>    3. ### Sonnet vs Haiku 이중 구조
>   
>    4. | 시나리오 | 모델 | 이유 |
>    5. |----------|------|------|
>    6. | 텍스트 카드뉴스 생성 | claude-sonnet-4 (기본) | 품질 우선 |
>    7. | 이미지 OCR + 분석 | claude-sonnet-4 | Vision 입력 처리 |
>    8. | 비용 절감 모드 | claude-haiku-4-5 | 동일 구조 프롬프트에서 Haiku가 약 8배 저렴하고 품질 차이 최소 |
>    9. | 자동 일간 생성 | 환경변수 `MODEL`로 전환 가능 | 운영 비용 조절 |
>
>    10. Haiku는 구조가 단순하고 반복적인 "뉴스 요약 → 카드 5장" 패턴에서 Sonnet 대비 품질 손실이 거의 없어, 자동 생성 잡의 기본값으로 두고 운영 비용을 관리합니다.
>
>    11. ---
>
>    12. ## Project Structure
>
>    13. ```
>        cardnews_bot/
>        ├── server.py           # Flask 웹 서버 + 카드뉴스 스튜디오 + SNS 발행 API
>        ├── bot.py              # Telegram 봇 (수신 → Claude 생성 → 세션 저장)
>        ├── auto_jobs.py        # APScheduler 자동화 잡 (일별 생성 + 예약 발행)
>        ├── auto_scheduler.py   # 스케줄러 프로세스 진입점
>        ├── article_images.py   # 기사 이미지 검색 · 캐싱
>        ├── code_agent_bot.py   # 원격 코딩봇 (선택)
>        ├── headless_publish.py # Playwright 헤드리스 발행 실행기
>        ├── cardnews_studio.html # 웹 편집 스튜디오 (single-page app)
>        ├── auto_studio.html    # 자동화 대시보드 UI
>        ├── data/
>        │   └── tooltip_topics.yaml  # 툴팁 주제 풀
>        ├── sessions/           # 생성된 카드뉴스 세션 (git 제외)
>        ├── uploads/            # 렌더링된 PNG (git 제외)
>        ├── .env.example        # 환경변수 템플릿
>        └── requirements.txt
>        ```
>
> ---
>
> ## Quick Start
>
> ### 1. 환경 설정
>
> ```bash
> git clone https://github.com/your-username/cardnews-bot.git
> cd cardnews-bot
>
> python -m venv venv
> source venv/bin/activate  # Windows: venv\Scripts\Activate.ps1
>
> pip install -r requirements.txt
> playwright install chromium  # 헤드리스 발행용
> ```
>
> ### 2. 환경변수 설정
>
> ```bash
> cp .env.example .env
> ```
>
> `.env`에서 아래 항목을 채웁니다.
>
> ```dotenv
> # 필수
> TELEGRAM_BOT_TOKEN=   # @BotFather에서 발급
> ANTHROPIC_API_KEY=    # console.anthropic.com
> WEB_USERNAME=admin
> WEB_PASSWORD=
> SECRET_KEY=           # secrets.token_hex(32)
>
> # 선택 — SNS 발행 사용 시
> IG_USER_ID=
> IG_ACCESS_TOKEN=
> FB_PAGE_ACCESS_TOKEN=
> THREADS_ACCESS_TOKEN=
> SERVER_URL=https://your-domain.com  # 외부 접근 가능한 HTTPS URL (Instagram 필수)
> ```
>
> ### 3. 실행
>
> ```bash
> # 터미널 1 — 웹 서버 + 스튜디오
> python server.py
>
> # 터미널 2 — Telegram 봇
> python bot.py
>
> # 터미널 3 (선택) — 자동화 스케줄러
> python auto_scheduler.py
> ```
>
> 브라우저에서 `http://localhost:5050` 접속 → 스튜디오 확인.
>
> ### 4. 외부 URL 노출 (SNS 발행 필수)
>
> Instagram Graph API는 공개 HTTPS URL을 요구합니다.
>
> ```bash
> # Cloudflare Tunnel (무료, 권장)
> cloudflared tunnel --url http://localhost:5050
>
> # 또는 ngrok
> ngrok http 5050
> ```
>
> 발급된 URL을 `.env`의 `SERVER_URL`에 설정 후 서버 재시작.
>
> ---
>
> ## Environment Variables
>
> | 변수 | 필수 | 설명 |
> |------|------|------|
> | `TELEGRAM_BOT_TOKEN` | ✅ | Telegram 봇 토큰 |
> | `ANTHROPIC_API_KEY` | ✅ | Claude API 키 |
> | `WEB_USERNAME` / `WEB_PASSWORD` | ✅ | 스튜디오 로그인 |
> | `SECRET_KEY` | ✅ | Flask 세션 서명 키 |
> | `SERVER_URL` | SNS 발행 시 ✅ | 외부 HTTPS 서버 주소 |
> | `ALLOWED_USERS` | 권장 | 봇 허용 Telegram user ID (콤마 구분) |
> | `IG_USER_ID` / `IG_ACCESS_TOKEN` | 선택 | Instagram 발행 |
> | `FB_PAGE_ACCESS_TOKEN` | 선택 | Facebook 발행 |
> | `THREADS_ACCESS_TOKEN` | 선택 | Threads 발행 |
> | `MODEL` | 선택 | Claude 모델 (기본: `claude-sonnet-4-20250514`) |
> | `AUTO_PUBLISH` | 선택 | 자동 발행 on/off (기본: `true`) |
>
> 전체 목록은 `.env.example` 참고.
>
> ---
>
> ## Usage
>
> ### Telegram 봇
>
> | 입력 | 결과 |
> |------|------|
> | 텍스트 메시지 | 내용 분석 → 카드뉴스 5–7장 생성 |
> | 사진 1–5장 | OCR + 이미지 분석 → 카드뉴스 생성 |
> | `[MZ]` `[감성]` 등 태그 + 메시지 | 해당 톤으로 생성 |
> | `/list` | 최근 생성 세션 목록 |
> | `/last` | 마지막 세션 스튜디오 링크 |
> | `/whoami` | 본인 Telegram user ID 확인 |
>
> ---
>
> ## 비용 참고
>
> | 시나리오 | 모델 | 1회 |
> |----------|------|-----|
> | 텍스트 | Sonnet 4 | ~$0.015 |
> | 이미지 1장 | Sonnet 4 | ~$0.03 |
> | 이미지 5장 | Sonnet 4 | ~$0.08 |
> | 텍스트 | Haiku 4.5 | ~$0.002 |
>
> ---
>
> ## License
>
> MIT

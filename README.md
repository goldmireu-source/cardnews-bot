# Cardnews Bot — 설치 & 실행 가이드

텔레그램에 **텍스트 또는 이미지**를 보내면, Claude가 분석해서 카드뉴스 5-7장으로 자동 생성합니다.
이미지는 안의 텍스트(OCR), 표, 차트, 손글씨까지 모두 읽어요.

```
┌─ 텔레그램 ──────────────────────┐    ┌─ 웹 스튜디오 (편집/다운로드) ──┐
│ 1. 텍스트나 사진 전송           │    │                                 │
│ 2. 봇이 카드뉴스 생성           │ ─▶ │ • 카드별 편집                   │
│ 3. 미리보기 URL 응답           │    │ • 레이아웃 변경                 │
└─────────────────────────────────┘    │ • PNG 일괄 다운로드             │
                                       └─────────────────────────────────┘
```

---

## 0. 준비물 체크

- [ ] **컴퓨터** (Mac / Windows / Linux) — 봇과 서버를 돌릴 곳
- [ ] **Python 3.10 이상** ([python.org](https://python.org)에서 다운로드)
- [ ] **텔레그램 계정** — 봇과 대화할 본인 계정
- [ ] **이메일** — Anthropic 가입용

설치 안 된 게 있으면 먼저 설치하고 오세요. 30분 안 걸립니다.

---

## 1. 텔레그램 봇 만들기

### 1-1. BotFather에서 토큰 받기

1. 텔레그램 앱 열고 검색창에 **`@BotFather`** 입력
2. 파란 체크가 붙은 **BotFather** 선택 → 시작(Start)
3. 메시지 입력란에 `/newbot` 입력해서 전송
4. BotFather가 물어보는 순서:
   - **봇 이름**: 사람들에게 보일 이름. 예: `미르 카드뉴스`
   - **봇 핸들**: `_bot` 으로 끝나야 함. 예: `mir_cardnews_bot`
5. 성공하면 이런 메시지가 옴:
   ```
   Done! Congratulations on your new bot.
   ...
   Use this token to access the HTTP API:
   123456789:ABCdef-GHIjklmnopQRStu_vwXYZ
   ```
6. **저 토큰을 복사해서 메모장에 임시 저장**. 나중에 `.env` 파일에 붙여넣을 거예요.

> 💡 토큰은 절대 공개하지 마세요. 깃허브에 올리거나 누구한테 보여주면 봇이 탈취됩니다.

### 1-2. 본인 봇 찾기

BotFather가 답장에 `t.me/mir_cardnews_bot` 같은 링크를 줘요. 클릭하면 본인의 봇으로 이동합니다. 일단 **/start** 한 번 눌러두세요 (대화 시작 상태로).

---

## 2. Claude API 키 받기

1. [console.anthropic.com](https://console.anthropic.com) 접속 → 가입
2. 결제 수단 등록 → **크레딧 충전** (최소 $5부터)
   - 카드뉴스 한 번 생성에 약 $0.01-0.03이라 $5면 200번 정도 가능
3. 좌측 메뉴 **API Keys** → **Create Key**
4. 이름은 아무거나 (`cardnews-bot` 추천), Create
5. `sk-ant-api03-...` 형식의 키가 나옴 → **메모장에 복사**

> 💡 키도 절대 공개하지 마세요. 다시 보여주지 않으니 안전한 곳에 저장.

---

## 3. 파일 다운로드 & 폴더 정리

이 zip 파일 받았으면 압축을 풉니다.

```
cardnews_bot/
├── bot.py                  ← 텔레그램 봇
├── server.py               ← 웹 서버
├── cardnews_studio.html    ← 카드뉴스 스튜디오
├── requirements.txt        ← Python 패키지 목록
├── .env.example            ← 환경변수 템플릿
└── README.md               ← 이 파일
```

원하는 위치에 두세요. 예: `~/projects/cardnews_bot/` 또는 `D:\projects\cardnews_bot\`

---

## 4. Python 환경 설정

### 4-1. 터미널 열기

- **Mac**: Spotlight(`⌘+Space`) → `terminal`
- **Windows**: 시작 → `cmd` 또는 `PowerShell`
- **VS Code**: `View → Terminal`

### 4-2. 폴더로 이동

```bash
cd ~/projects/cardnews_bot
# 또는 Windows:
cd D:\projects\cardnews_bot
```

### 4-3. 가상환경 만들기

가상환경 = 이 프로젝트 전용 Python 패키지 공간. 다른 프로젝트와 안 섞입니다.

```bash
python -m venv venv
# 또는 (Mac에서 python이 안되면)
python3 -m venv venv
```

### 4-4. 가상환경 활성화

**Mac/Linux:**
```bash
source venv/bin/activate
```

**Windows (PowerShell):**
```powershell
venv\Scripts\Activate.ps1
```

**Windows (cmd):**
```cmd
venv\Scripts\activate.bat
```

활성화되면 프롬프트 앞에 `(venv)` 가 붙어요.

### 4-5. 패키지 설치

```bash
pip install -r requirements.txt
```

설치 끝나면 `Successfully installed ...` 메시지가 뜹니다.

---

## 5. 환경변수 설정 (.env)

### 5-1. .env 파일 만들기

`.env.example` 을 복사해서 `.env` 라는 이름으로 만듭니다.

**Mac/Linux:**
```bash
cp .env.example .env
```

**Windows:**
```cmd
copy .env.example .env
```

### 5-2. .env 파일 열어서 수정

메모장이나 VS Code로 `.env` 파일을 열고 다음 두 줄을 본인 것으로 채웁니다:

```
TELEGRAM_BOT_TOKEN=123456789:ABCdef-GHIjklmnopQRStu_vwXYZ
ANTHROPIC_API_KEY=sk-ant-api03-여기에본인키
```

나머지(`SERVER_URL`, `ALLOWED_USERS` 등)는 일단 기본값 그대로 두세요.

저장.

---

## 6. 실행 (터미널 2개 필요)

### 6-1. 첫 번째 터미널 — 웹 서버

```bash
# 가상환경이 활성화된 상태에서
python server.py
```

성공하면:
```
🌐 Cardnews Studio Server
   http://localhost:5050
   세션 폴더: /Users/.../cardnews_bot/sessions
```

브라우저에서 [http://localhost:5050](http://localhost:5050) 열어서 스튜디오가 뜨면 OK.

### 6-2. 두 번째 터미널 — 봇

**같은 폴더에서** 터미널을 하나 더 열고:

```bash
# 가상환경 활성화 다시 필요
source venv/bin/activate           # Mac/Linux
# venv\Scripts\Activate.ps1        # Windows

python bot.py
```

성공하면:
```
🤖 Cardnews Bot 시작
📦 모델: claude-sonnet-4-20250514
📂 세션: /Users/.../cardnews_bot/sessions
🌐 스튜디오: http://localhost:5050
⚠️ ALLOWED_USERS 미설정 - 누구나 봇을 쓸 수 있음
```

> ⚠️ "누구나 봇을 쓸 수 있음" 경고 — 봇 핸들이 알려지면 다른 사람도 쓸 수 있고 본인 Anthropic 크레딧이 깎입니다. 다음 단계에서 본인만 쓸 수 있게 잠급니다.

---

## 7. 본인만 쓸 수 있게 잠그기 (강력 권장)

1. 텔레그램에서 본인 봇으로 가서 `/whoami` 입력
2. 봇이 응답:
   ```
   user_id: 987654321
   username: @your_handle
   
   ALLOWED_USERS에 추가하려면 .env에:
   ALLOWED_USERS=987654321
   ```
3. `.env` 파일 열어서 `ALLOWED_USERS=987654321` 로 수정 (본인 ID로)
4. 두 번째 터미널의 봇 멈춤 (`Ctrl+C`) → `python bot.py` 다시 시작
5. 이제 다른 사람이 봇에 메시지 보내도 `⛔ 권한 없음` 응답

> 가족·동료와 공유하고 싶으면 콤마로: `ALLOWED_USERS=987654321,111222333`

---

## 8. 실제로 써보기

> ⚠️ **반드시 [http://localhost:5050](http://localhost:5050) 으로 접속하세요.** HTML 파일을 더블클릭해서 열면 (`file://...`) 브라우저 보안 때문에 AI 생성이 안 됩니다.

### 8-1. 텍스트로 만들기

봇 대화창에 그냥 내용을 길게 써서 보내세요:

```
인공지능 사관학교 8기 첫 주 회고

1주차에는 Python 기초부터 시작했어요. 변수, 자료형, 제어문을 다시 짚고
가는 시간이었는데 생각보다 신선했어요.

특히 좋았던 점:
- 기초를 다시 잡으니 다음 주 진도가 편함
- 코드 리뷰 문화가 자연스럽게 정착됨
- 팀 분위기가 활발해서 질문하기 좋음

다음 주에는 데이터 분석으로 들어갑니다. 기대해 주세요!
```

봇 응답:
```
🎨 카드뉴스 생성 중... (톤: 친근한 정보 전달)
↓ (10-20초)
✅ 6장 생성 완료 · 테마 navy · 텍스트

1. [cover/bold] 인공지능 사관학교 첫 주
2. [topic/centered] 1주차에 배운 것
3. [list/check] 특히 좋았던 점
...

📱 편집/다운로드:
http://localhost:5050/?session=tg_1234567
```

링크 클릭 → 스튜디오에서 카드 확인 → 편집/다운로드.

### 8-2. 이미지로 만들기

봇 대화창에서 **📎 첨부 → 갤러리 또는 카메라** → 사진 보내기.

이런 게 잘 됩니다:
- 손글씨 메모 사진
- 노트 필기 스캔
- 발표 슬라이드 캡처
- 책/잡지 페이지
- 그래프나 표가 있는 자료
- 강의 화이트보드 사진

봇이 OCR + 분석 후 카드뉴스로 변환합니다.

### 8-3. 이미지 여러 장 + 캡션

여러 장 골라서 보낼 때 첫 사진에 캡션을 달면:
- 캡션 = 추가 컨텍스트 (Claude한테 보충 설명)
- 이미지 = 메인 소스
- 최대 5장까지 한 번에 분석

### 8-4. 톤 변경

메시지나 캡션 맨 앞에 태그:
- `[MZ]` — MZ 톤, 위트있게
- `[감성]` — 감성적·따뜻하게
- `[진지]` — 진지하고 신뢰감
- `[핵심]` — 핵심만 실용적으로
- `[친근]` — 친근한 정보 전달 (기본)

예: `[감성] 오늘 수업 끝나고 카페에서 든 생각...`

### 8-5. 명령어

| 명령 | 동작 |
|------|------|
| `/start` `/help` | 도움말 |
| `/whoami` | 내 user_id 확인 |
| `/list` | 최근 만든 카드뉴스 목록 |
| `/last` | 마지막 카드뉴스 미리보기 링크 |

---

## 9. 외부에서 접속 (선택)

기본은 `localhost` 라 같은 PC에서만 됩니다. 카페에서 폰으로 편집하고 싶으면 외부 URL 노출 필요.

### 옵션 A: Cloudflare Tunnel (무료·안정·추천)

**Mac:**
```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:5050
```

**Windows:** [cloudflared 다운로드](https://github.com/cloudflare/cloudflared/releases/latest) → 압축 풀고:
```cmd
cloudflared.exe tunnel --url http://localhost:5050
```

출력에 `https://random-words.trycloudflare.com` 같은 URL이 뜸 → 복사.

### 옵션 B: ngrok (간단)

[ngrok.com](https://ngrok.com) 가입 → 다운로드 → 토큰 등록 후:
```bash
ngrok http 5050
```
→ `https://abcd1234.ngrok-free.app` URL 받음.

### 적용

`.env` 의 `SERVER_URL` 을 받은 URL로 변경:
```
SERVER_URL=https://random-words.trycloudflare.com
```

봇 재시작 (`Ctrl+C` → `python bot.py`).

이제 봇 응답의 미리보기 URL이 그 도메인으로 옵니다. 폰 브라우저에서 열기 가능.

> Cloudflare Tunnel URL 은 매번 바뀌어요. 고정 URL 원하면 Cloudflare에서 도메인 등록 후 `named tunnel` 사용.

---

## 10. 매번 실행하는 법

설치는 한 번이고, 이후엔:

**Mac/Linux:**
```bash
cd ~/projects/cardnews_bot
source venv/bin/activate
python server.py        # 터미널 1
# 새 터미널 열고 같은 폴더에서:
source venv/bin/activate
python bot.py           # 터미널 2
```

**Windows:**
```cmd
cd D:\projects\cardnews_bot
venv\Scripts\activate
python server.py        :: 터미널 1
:: 새 터미널 열고:
venv\Scripts\activate
python bot.py           :: 터미널 2
```

종료: 각 터미널에서 `Ctrl+C`.

---

## 11. 트러블슈팅

### "python: command not found"
→ Python 미설치. [python.org](https://python.org)에서 설치. Windows 설치 시 "Add Python to PATH" 체크 필수.

### "pip: command not found"
→ 가상환경 활성화 안 됨. `source venv/bin/activate` (Mac) 또는 `venv\Scripts\activate` (Win) 다시 실행.

### "Failed to fetch" — 스튜디오에서 AI 생성 클릭 시
이건 거의 항상 다음 두 가지 중 하나:
1. **HTML 파일을 더블클릭해서 열었음** (file://) — 브라우저가 보안상 외부 API 호출을 막아요. 반드시 `python server.py` 켜놓고 `http://localhost:5050` 으로 접속하세요.
2. **`python server.py` 가 안 켜져 있음** — 두 번째 터미널에서 서버 실행 중인지 확인.

스튜디오는 서버를 거쳐서 Claude API를 호출합니다 (CORS 회피 + API 키 보안).

### "ANTHROPIC_API_KEY 미설정" 응답
`.env` 파일에 키 추가 후 `python server.py` 재시작 필요. `.env` 가 봇뿐 아니라 서버에서도 필요해요.

### "Telegram error: Unauthorized"
→ `.env` 의 `TELEGRAM_BOT_TOKEN` 잘못됨. BotFather에서 다시 확인.

### "anthropic.AuthenticationError"
→ `ANTHROPIC_API_KEY` 잘못됨. console.anthropic.com에서 키 재확인.

### "anthropic.RateLimitError" or "credit_balance_too_low"
→ Anthropic 크레딧 부족. console.anthropic.com → Billing에서 충전.

### 봇이 응답 없음
- 두 번째 터미널의 `python bot.py` 로그 확인 (에러 메시지 있는지)
- 봇 토큰을 같은 시간에 다른 곳에서 같이 쓰면 polling 충돌남. 한 곳에서만 실행.

### 미리보기 링크 클릭하면 "세션 없음"
- `python server.py` 가 켜져 있는지 확인
- `.env` 의 `SERVER_URL` 이 실제 서버 주소와 일치하는지

### JSON 파싱 오류
- 응답이 잘림 (max_tokens 한계). 이미지 수를 줄이거나 내용을 짧게.
- `bot.py` 의 `max_tokens=4096` 을 더 큰 값으로 변경 가능.

### 이미지가 너무 크다는 에러
- Claude API는 이미지 5MB 한계. 폰에서 보낼 땐 자동 압축되니 보통 OK.
- 그래도 안 되면 사진을 미리 크롭/리사이즈.

---

## 12. 자주 수정하는 곳

### 새 톤 추가
`bot.py` 상단:
```python
TONE_MAP = {
    "[MZ]":   "MZ세대 톤, 위트있게",
    "[전문]": "B2B 마케터 톤, 데이터 중심",   # ← 추가
    ...
}
```
스튜디오 v4에도 같은 톤 추가하려면 `cardnews_studio.html` 의 `openOneShotModal` 안 `<select id="opt-tone">` 에 `<option>` 한 줄 추가.

### 브랜드 기본값
`bot.py` 상단 `DEFAULT_BRAND` 와 `cardnews_studio.html` 의 `STATE.brand` 둘 다 수정.

### 모델 교체 (저렴하게)
`.env`:
```
MODEL=claude-haiku-4-5-20251001
```
Haiku는 1/10 비용. 다만 카드 구조 품질이 약간 낮을 수 있음.

### 새 명령어 추가
`bot.py` 의 `main()` 안:
```python
async def cmd_stats(update, ctx):
    n = len(list_sessions(limit=1000))
    await update.message.reply_text(f"총 {n}개 생성됨")

# main() 안에
app.add_handler(CommandHandler("stats", cmd_stats))
```

### system prompt 수정 (출력 결과 조정)
`bot.py` 의 `build_system_prompt()` 와 `cardnews_studio.html` 의 동일 부분.
규칙 추가/제거 가능:
- "항상 6장 이상으로 만들 것"
- "통계 카드는 1장만"
- "한국어 줄바꿈을 더 짧게"

---

## 13. Studio Hub 통합 (이미 운영 중이라면)

미르 씨가 이미 Studio Hub 운영 중이라면:

1. `server.py` 의 라우트를 Studio Hub Flask 앱에 `/cardnews/` prefix 로 흡수
2. `bot.py` 는 별도 프로세스 유지 (텔레그램 polling 이 블로킹)
3. `sessions/` 폴더만 Studio Hub 의 데이터 디렉토리로 이동
4. `cardnews_studio.html` 은 Studio Hub static 폴더로

그러면 기존 Gemini 흐름과 나란히, Claude 카드뉴스 흐름이 한 대시보드에서 작동합니다.

---

## 14. 비용 감각

| 시나리오 | 모델 | 1회 비용 | 월 30회 |
|---------|------|---------|---------|
| 텍스트만 | Sonnet 4 | ~$0.015 | ~$0.45 |
| 이미지 1장 + 텍스트 | Sonnet 4 | ~$0.03 | ~$0.90 |
| 이미지 5장 | Sonnet 4 | ~$0.08 | ~$2.40 |
| 텍스트만 | Haiku 4.5 | ~$0.002 | ~$0.06 |

부담 없이 쓸 수 있는 수준이에요. $5 충전이면 한참 갑니다.

---

## 마무리

설치 다 됐으면 한 번 사진 한 장으로 테스트해보세요. 강의 노트나 발표 자료 사진 보내면 어떤 카드가 나오는지 보면 감이 옵니다.

봇 응답 톤이나 카드 결과가 어색하면 `build_system_prompt()` 안 규칙을 조정하면 점점 미르 씨 스타일에 맞아갑니다.

좋은 콘텐츠 많이 만드시길!

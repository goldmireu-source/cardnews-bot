# Instagram Graph API 자동 게시 셋업

이 문서는 인스타 카드뉴스 자동 게시 기능을 켜기 위한 일회성 셋업 절차다.
작업이 외부 시스템(Meta, Facebook, ngrok 등)에 의존하므로 순서대로 진행할 것.

## 0. 사전 준비

- Instagram **개인 계정 → 크리에이터 또는 비즈니스 계정으로 전환** (앱에서 `설정 → 계정 → 프로페셔널 계정으로 전환`)
- Facebook **페이지** 1개. 없으면 [facebook.com/pages/create](https://www.facebook.com/pages/create) 에서 생성
- Facebook 페이지와 인스타 계정 **연결**: 인스타 앱 `설정 → 비즈니스 → Facebook 페이지 연결`
- 같은 Facebook 계정으로 [developers.facebook.com](https://developers.facebook.com/) 로그인

## 1. Meta 앱 생성

1. [My Apps](https://developers.facebook.com/apps/) → **Create App**
2. App type: **Business** 선택
3. App name 입력 (예: "AI News Card Poster") → Email 입력 → Create
4. App 대시보드 좌측 사이드바에서 **Add Product** → **Instagram Graph API** Set Up
5. 다시 **Add Product** → **Facebook Login for Business** Set Up

## 2. 권한 요청 (App Review)

좌측 사이드바 → **App Review → Permissions and Features**

다음 3개 권한이 필요:

- `instagram_basic` — 인스타 계정 정보 읽기
- `instagram_content_publish` — 카드뉴스 게시
- `pages_show_list` — 연결된 페이지 조회

**개발 모드** 에서는 본인 계정만 사용 가능하지만 권한 활성화 즉시 동작함. 본인만 운영할 거면 App Review 통과 안 해도 OK. 외부 사용자에게 공개할 거면 App Review 필요.

## 3. 토큰 발급

Graph API Explorer 사용:

1. [developers.facebook.com/tools/explorer](https://developers.facebook.com/tools/explorer/) 접속
2. 우상단 App 드롭다운에서 방금 만든 앱 선택
3. **User or Page** 드롭다운 → **Get User Access Token**
4. Permissions 에 위 3개 권한 체크 → **Generate Access Token**
5. 발급된 단기 토큰 복사 (1~2시간 만료)

**Long-lived Page Token** 으로 변환:

```bash
# 1) 단기 user → 장기 user 토큰 (60일)
curl -G "https://graph.facebook.com/v21.0/oauth/access_token" \
  -d grant_type=fb_exchange_token \
  -d client_id={YOUR_APP_ID} \
  -d client_secret={YOUR_APP_SECRET} \
  -d fb_exchange_token={SHORT_USER_TOKEN}
# → 응답에서 access_token 추출

# 2) 페이지 ID 와 페이지 토큰 조회 (절대 만료 안 됨)
curl "https://graph.facebook.com/v21.0/me/accounts?access_token={LONG_USER_TOKEN}"
# → data[].id (페이지ID) 와 data[].access_token (페이지 토큰) 추출
```

PowerShell 대안:
```powershell
$short = "{SHORT_USER_TOKEN}"
$appId = "{APP_ID}"
$appSecret = "{APP_SECRET}"
$long = (Invoke-RestMethod "https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token&client_id=$appId&client_secret=$appSecret&fb_exchange_token=$short").access_token
$pages = (Invoke-RestMethod "https://graph.facebook.com/v21.0/me/accounts?access_token=$long").data
$pages | Format-Table id, name, access_token
```

## 4. Instagram User ID 조회

```bash
# 페이지 ID + 페이지 토큰으로 연결된 IG 계정 ID 조회
curl "https://graph.facebook.com/v21.0/{PAGE_ID}?fields=instagram_business_account&access_token={PAGE_TOKEN}"
# → instagram_business_account.id 값이 IG_USER_ID
```

## 5. 이미지 외부 접근 — PUBLIC_BASE_URL

Instagram Graph API 는 슬라이드 PNG 를 직접 fetch 하므로 외부에서 접근 가능한 https URL 이 필요.
로컬 개발 환경이라면 ngrok 또는 cloudflared 추천.

### ngrok 사용

1. [ngrok.com](https://ngrok.com/) 가입 + 토큰 발급
2. `ngrok config add-authtoken {TOKEN}`
3. Flask 앱 가동 후:
   ```powershell
   ngrok http 5001
   ```
4. 출력에서 `https://xxxxxx.ngrok-free.app` 같은 URL 복사 → `.env` 의 `PUBLIC_BASE_URL` 에 설정

### cloudflared (무료, 인증 없음)

```powershell
cloudflared tunnel --url http://localhost:5001
```

> ⚠️ 무료 ngrok URL 은 재시작마다 바뀜. 고정 URL 이 필요하면 ngrok 유료, 또는 도메인 보유 시 cloudflare tunnel 정식 설정.

## 6. .env 설정

```env
IG_USER_ID=17841412345678901
IG_ACCESS_TOKEN=EAAJaZ...{매우_긴_토큰}
IG_FB_PAGE_ID=104567890123456
PUBLIC_BASE_URL=https://abc123.ngrok-free.app

# 자동 게시 여부 — true 면 큐에 쌓인 카드를 사람이 승인해야 게시 (안전), false 면 즉시 자동
INSTA_REQUIRE_APPROVAL=true

# 게시 시간대 (KST 시각)
INSTA_NEWS_POST_HOUR=12
INSTA_TOOLTIP_POST_HOUR=19

# 브랜드 식별
INSTA_BRAND_NAME=AI 데일리
INSTA_BRAND_HANDLE=@your_handle
```

`.env` 수정 후 **앱 재시작 필요** (Werkzeug 가 .env 안 watch).

## 7. 동작 확인

1. http://localhost:5001/admin/instagram 접속
2. 상단에 "Graph API 설정 완료" 초록 표시되는지 확인
3. **+ 오늘의 AI 툴팁 생성** 클릭 → draft 생성
4. **↻ draft 일괄 렌더** 클릭 → ready 상태로 전환 (몇 초)
5. 카드 미리보기 확인
6. 카드의 **상세** → 캡션/해시태그 검토 → **▶ 지금 게시** 클릭
7. 30초~수 분 후 인스타 앱에서 게시물 확인

성공하면 자동 스케줄이 매일 07:30 KST 에 카드를 생성하고 12:00 (뉴스) / 19:00 (툴팁) 에 게시함.

## 8. 토큰 만료

- 페이지 토큰은 **만료 없음** (소유 페이지 그대로면).
- 사용자 long-lived 토큰은 60일. 60일 안에 한 번 API 호출되면 자동 갱신됨.
- 토큰 만료 시 게시가 실패하므로 admin 페이지의 "failed" 항목에 에러 메시지 확인.

## 트러블슈팅

| 증상 | 원인 / 해결 |
|------|--------|
| `(#10) Application does not have permission for this action` | App Review 미통과 또는 권한 미요청 — 단계 2 다시 |
| `image_url is not accessible` | PUBLIC_BASE_URL 잘못됨. https 인지, ngrok 살아있는지 확인 |
| `(#100) Invalid parameter` (카드 컨테이너) | 슬라이드가 1080x1080 정사각형인지, 8MB 미만인지 확인 |
| `(#190) Error validating access token` | 토큰 만료 — 단계 3 부터 재발급 |
| `Application request limit reached` | 일일 200건 한도 도달 (개발 모드). App Review 통과 시 한도 상승 |

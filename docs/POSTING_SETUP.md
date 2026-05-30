# 멀티 플랫폼 발행 셋업 (Instagram · Facebook · Threads)

이 문서는 카드뉴스를 3개 플랫폼에 자동 발행하기 위한 일회성 토큰 발급 절차다.
플랫폼마다 별도 토큰이 필요하며, 어느 하나만 설정해도 그 플랫폼만 발행 가능하다 (부분 활성 OK).

> ⚠️ **공통 전제**: 모든 플랫폼이 이미지를 외부 https URL 에서 fetch 함.
> 로컬 개발 시 ngrok 또는 Cloudflare Tunnel 로 노출 후 `.env` 의 `SERVER_URL` 갱신.

## 0. 사전 준비 (3개 플랫폼 공통)

1. **Facebook 페이지** 1개 (개인 계정 직접 게시 API 없음)
2. **Instagram 비즈니스/크리에이터** 계정 → Facebook 페이지에 연결
3. **Threads 계정** — 인스타와 같은 계정으로 가입 시 자동
4. [developers.facebook.com](https://developers.facebook.com/) 에 같은 Facebook 계정으로 로그인

## 1. Instagram + Facebook 페이지 (하나의 Meta 앱)

이 둘은 **같은 앱**으로 처리 가능.

### 1-A. Meta 앱 만들기 (없을 때만)

1. [My Apps](https://developers.facebook.com/apps/) → **Create App**
2. App type: **Business**
3. 앱 이름 입력 → Create
4. 좌측 사이드바 → **Add Product**:
   - **Instagram Graph API** (Set Up)
   - **Facebook Login for Business** (Set Up)

### 1-B. 권한 요청

좌측 → **App Review → Permissions and Features** 에서 다음 권한 요청:

- `instagram_basic` — 인스타 기본 정보
- `instagram_content_publish` — 인스타 게시
- `pages_show_list` — 페이지 목록
- `pages_read_engagement` — 페이지 정보 읽기
- `pages_manage_posts` — **FB 페이지에 게시 (필수)**

본인 계정만 사용할 거면 App Review 통과 불필요 (개발 모드로 충분).

### 1-C. 토큰 발급

1. [Graph API Explorer](https://developers.facebook.com/tools/explorer/) 접속
2. 우상단 App 드롭다운 → 위 앱 선택
3. **Get User Access Token** → 위 5개 권한 체크 → Generate
4. 발급된 단기 토큰 복사 (1시간)

장기 토큰으로 변환 (PowerShell):
```powershell
$short = "{SHORT_TOKEN}"
$appId = "{APP_ID}"
$appSecret = "{APP_SECRET}"
$long = (Invoke-RestMethod "https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token&client_id=$appId&client_secret=$appSecret&fb_exchange_token=$short").access_token
Write-Host "Long-lived USER token: $long"

# 페이지 목록 + 페이지 토큰 (만료 없음)
$pages = (Invoke-RestMethod "https://graph.facebook.com/v21.0/me/accounts?access_token=$long").data
$pages | Format-Table id, name, access_token
```

위에서 받은 **페이지 토큰** + **페이지 ID** 가 인스타와 페북 발행 모두에 사용됩니다.

### 1-D. Instagram User ID 조회

```powershell
$pageId = "{FB_PAGE_ID}"
$pageToken = "{PAGE_TOKEN}"
$ig = (Invoke-RestMethod "https://graph.facebook.com/v21.0/$pageId?fields=instagram_business_account&access_token=$pageToken").instagram_business_account.id
Write-Host "IG_USER_ID: $ig"
```

### 1-E. .env 설정

```env
IG_USER_ID=17841234567890123
IG_ACCESS_TOKEN=EAAJaZ...{페이지 토큰}
FB_PAGE_ID=10456789012345
FB_PAGE_ACCESS_TOKEN=EAAJaZ...{페이지 토큰 — IG_ACCESS_TOKEN 과 동일}
```

> 💡 **인스타 토큰과 FB 페이지 토큰은 동일** 합니다 (같은 페이지 토큰). 따로 발급할 필요 없음.

## 2. Threads (별도 앱)

Threads API 는 **인스타·페북과 다른 앱**으로 등록해야 합니다.

### 2-A. Threads 앱 만들기

1. [My Apps](https://developers.facebook.com/apps/) → **Create App**
2. App type: **Consumer** (Threads 는 Consumer 앱)
3. 앱 이름 입력 → Create
4. 좌측 → **Add Product** → **Threads API** Set Up

### 2-B. OAuth Redirect URI 등록

1. Threads API 설정 → **Use Cases** → Configure
2. **Valid OAuth Redirect URI** 에 임시 콜백 추가:
   - 로컬: `https://localhost/oauth/callback` 또는 ngrok URL
   - 토큰 한 번만 받고 끝나므로 임시여도 OK

### 2-C. 권한 요청

- `threads_basic` (기본)
- `threads_content_publish` (게시)

본인 계정만 사용할 거면 개발 모드 OK (App Review 불필요).

### 2-D. 토큰 발급 (OAuth 흐름)

브라우저에서 다음 URL 접속 (값 채워서):

```
https://threads.net/oauth/authorize
  ?client_id={THREADS_APP_ID}
  &redirect_uri={REDIRECT_URI}
  &scope=threads_basic,threads_content_publish
  &response_type=code
```

승인 후 redirect URI 에 `?code=XXX` 가 붙음. 그 코드로 토큰 교환:

```powershell
$clientId = "{THREADS_APP_ID}"
$clientSecret = "{THREADS_APP_SECRET}"
$redirectUri = "{REDIRECT_URI}"
$code = "{REDIRECT_의_CODE}"

# 1) 단기 토큰
$short = Invoke-RestMethod -Method POST -Uri "https://graph.threads.net/oauth/access_token" -Body @{
  client_id = $clientId
  client_secret = $clientSecret
  grant_type = "authorization_code"
  redirect_uri = $redirectUri
  code = $code
}
Write-Host "short token: $($short.access_token), user_id: $($short.user_id)"

# 2) 장기 토큰 (60일)
$long = Invoke-RestMethod "https://graph.threads.net/access_token?grant_type=th_exchange_token&client_secret=$clientSecret&access_token=$($short.access_token)"
Write-Host "long-lived token: $($long.access_token)"
```

### 2-E. .env 설정

```env
THREADS_USER_ID=12345678901234567
THREADS_ACCESS_TOKEN=THQAS...{60일 long-lived}
```

> 60일마다 토큰이 만료됨. 사용 직전 자동 갱신은 `services/threads.py` 의 `refresh_long_lived_token()` 에 구현돼 있음 (수동 호출 또는 cron 추가 가능).

## 3. TikTok (별도 앱 + 자체 도메인 필수)

TikTok Content Posting API 는 **PHOTO 모드에서 PULL_FROM_URL 만 지원**하고,
TikTok 이 도메인 소유 검증을 요구하므로 **자체 도메인이 필수**입니다
(`trycloudflare.com` 같은 임시 도메인 검증 불가).

### 3-A. 자체 도메인 + cloudflared named tunnel 준비

기존 quick tunnel 은 매번 도메인이 바뀌어서 TikTok 검증 불가. 다음 중 하나 선택:

**옵션 1) cloudflare 도메인 + named tunnel (Recommended)**

1. cloudflare 에 도메인 등록 (또는 기존 도메인의 네임서버를 cloudflare로 이전)
2. PowerShell:
   ```powershell
   cloudflared tunnel login                       # 브라우저 인증
   cloudflared tunnel create cardnews             # 터널 생성
   cloudflared tunnel route dns cardnews cards.yourdomain.com
   cloudflared tunnel run cardnews
   ```
3. `cards.yourdomain.com` 이 로컬 5050 으로 라우팅됨

**옵션 2) 다른 호스팅의 reverse proxy** — nginx + 본인 서버에 cardnews 로 라우팅

이후 `.env` 의 `SERVER_URL` 을 새 도메인으로 갱신:
```env
SERVER_URL=https://cards.yourdomain.com
```

### 3-B. TikTok 개발자 앱 만들기

1. [developers.tiktok.com](https://developers.tiktok.com/) 가입 + 로그인
2. Manage Apps → **Connect an app**
3. 앱 이름·아이콘·설명 입력 → 만들기
4. App detail → **Add products**:
   - **Login Kit**
   - **Content Posting API**

### 3-C. URL 설정 (Login Kit)

- **Redirect URI**: `https://cards.yourdomain.com/oauth/tiktok/callback` (임의 path 됨 — 한 번만 사용)
- **Web/Desktop URL**: `https://cards.yourdomain.com`
- **Terms of Service URL** / **Privacy Policy URL**: 같은 도메인 또는 임의 페이지

### 3-D. URL 정의 (Content Posting API)

- **Domain verification**: TikTok 콘솔에서 도메인 입력 → 메타 태그 또는 `tiktok-developers-site-verification.txt` 파일 제공
  → 그 파일을 `static/` 에 두면 `https://cards.yourdomain.com/static/...` 로 접근 가능 → 콘솔에서 Verify

### 3-E. 권한 (Scopes)

- `user.info.basic`
- `video.publish` ← PHOTO Direct Post 도 이 scope
- `video.upload` (선택 — MEDIA_UPLOAD inbox 모드도 쓸 거면)

> ⚠️ **Sandbox 상태**: 앱이 audit 통과 전엔 본인 계정만 발행 가능. 본인 카드뉴스 운영 용도면 OK — audit 신청 불필요.

### 3-F. 토큰 발급 (OAuth)

브라우저로 다음 URL 열기 (값 채워서):
```
https://www.tiktok.com/v2/auth/authorize/
  ?client_key={CLIENT_KEY}
  &scope=user.info.basic,video.publish
  &response_type=code
  &redirect_uri={REDIRECT_URI}
  &state=cardnews
```

승인 후 redirect URL 에 `?code=XXX&state=cardnews` 가 붙음. code 로 토큰 교환:

```powershell
$clientKey = "{CLIENT_KEY}"
$clientSecret = "{CLIENT_SECRET}"
$redirect = "{REDIRECT_URI}"
$code = "{REDIRECT_CODE}"

$body = @{
  client_key = $clientKey
  client_secret = $clientSecret
  code = $code
  grant_type = "authorization_code"
  redirect_uri = $redirect
}
$tok = Invoke-RestMethod -Method POST `
  -Uri "https://open.tiktokapis.com/v2/oauth/token/" `
  -ContentType "application/x-www-form-urlencoded" `
  -Body $body

"access_token: $($tok.access_token)"     # 24h
"refresh_token: $($tok.refresh_token)"   # 365d (재발급마다 새 refresh_token 받음)
"open_id: $($tok.open_id)"
```

### 3-G. `.env` 설정

```env
TIKTOK_CLIENT_KEY=awxxxxxxxxxxxxxxxx
TIKTOK_CLIENT_SECRET=xxxxxxxxxxxxxxxx
TIKTOK_ACCESS_TOKEN=act.xxxxxxxxxxxx
TIKTOK_REFRESH_TOKEN=rft.xxxxxxxxxxxx
TIKTOK_OPEN_ID=xxxxxxxx-xxxx-xxxx
```

> 💡 access_token 은 24시간 만료. 봇이 매일 03:00 KST 에 자동 갱신 (cron 등록됨). refresh_token 갱신 시 새 refresh_token 도 함께 받아서 갱신해야 함 — 토큰 갱신 잡이 자동 처리.

### 3-H. 운영상 제약 알아두기

- 분당 6회 / 24시간 pending 5개 게시 한도
- PHOTO 캐러셀 최대 35장
- 캡션: title 90자 + description 4000자 (봇이 viral caption 첫 줄 → title, 나머지 → description 자동 분할)
- 카드 비율: TikTok 권장 9:16 인데 4:5(1080×1350) 도 보임 (위/아래 살짝 패딩될 수 있음)

## 4. 공개 https URL — ngrok (필수)

Instagram·Facebook·Threads·TikTok 모두 이미지를 fetch 하려면 외부 https URL 필요.
TikTok 은 도메인 검증 때문에 자체 도메인 필수 — 그 도메인을 그대로 SERVER_URL 로 쓰면 됨 (3-A 참고).

```powershell
# ngrok 가입 + 토큰 발급 (https://ngrok.com)
ngrok config add-authtoken {YOUR_TOKEN}

# 서버 가동 후
ngrok http 5050
# 출력에서 https://xxxx.ngrok-free.app 복사
```

`.env` 에 추가:
```env
SERVER_URL=https://xxxx.ngrok-free.app
```

서버 재시작 → http://localhost:5050/auto 의 발행 큐에서 "발행 (인스타·FB·스레드)" 클릭 → 체크박스 셋업 완료된 플랫폼만 활성화돼 있음.

## 4. 동작 검증

```powershell
# 토큰 설정 상태 확인
Invoke-RestMethod http://localhost:5050/api/publish/status | ConvertTo-Json -Depth 4
```

기대 응답:
```json
{
  "server_url_https": true,
  "server_url": "https://xxxx.ngrok-free.app",
  "platforms": {
    "instagram": { "configured": true },
    "facebook":  { "configured": true },
    "threads":   { "configured": true }
  }
}
```

## 5. 트러블슈팅

| 증상 | 원인 / 해결 |
|------|------------|
| `(#10) Application does not have permission for this action` | 권한 누락 — App Review 다시 (`pages_manage_posts` 등) |
| `image_url is not accessible` | SERVER_URL https 아니거나 ngrok 끊김 |
| Threads `Invalid platform app` | Threads 는 **별도 앱**. 인스타 앱 ID/Secret 으로 시도하면 거부 |
| Threads `OAuthException invalid_grant` | code 만료 (5분) — 다시 authorize URL 부터 |
| `(#190) Invalid OAuth access token` | 토큰 만료. 1-C / 2-D 재실행 |
| 페북 페이지 게시는 되는데 인스타 안 됨 | 인스타 비즈니스 계정 ↔ 페이지 연결 확인. 페이지 설정 → Instagram |

## 토큰 만료·갱신 요약

| 플랫폼 | 토큰 종류 | 만료 |
|--------|-----------|------|
| Instagram | 페이지 토큰 | 페이지 소유 유지하면 만료 없음 |
| Facebook 페이지 | 페이지 토큰 (인스타와 동일) | 위와 동일 |
| Threads | long-lived user token | 60일, 사용 시 자동 갱신 가능 |
| TikTok | access_token + refresh_token | access 24h / refresh 365일, 봇이 매일 03:00 자동 갱신 |

# 포팅 노트 — 데일리싱크에서 추출한 인스타 자동화 코드

> 이 폴더의 코드는 원래 데일리싱크(F:\ai-news-digest\ai-news-digest) 프로젝트의
> Flask + SQLAlchemy + APScheduler 스택을 전제로 작성됐다.
> cardnews_bot 은 Telegram + Flask 단일파일 구조라서, 그대로 드롭인 되지 않는다.
> 아래 매핑/의존성을 따라 어댑팅 필요.

## 새로 추가된 파일 (이 폴더에 그대로 복사돼 있음)

| 파일 | 역할 | 핵심 외부 의존 |
|------|------|----------|
| `jobs/insta_news.py` | 데일리싱크 Cluster → Claude 로 인스타 슬라이드 + 캡션 + 해시태그 생성 | `models.InstaPost`, `models.Cluster`, `services.claude.generate_json` |
| `jobs/insta_tooltip.py` | YAML 주제 풀 → Claude 로 AI 툴 활용법 카드 생성 | `data/tooltip_topics.yaml`, `services.claude.generate_json` |
| `jobs/insta_render.py` | Playwright 로 HTML → 1080x1080 PNG | playwright, Flask test_client, `templates/insta/card.html` |
| `jobs/insta_pipeline.py` | 스케줄러 잡 3종 (generate / render / publish) | 위 3개 + `services.instagram` + `jobs.pipeline._track` (JobRun) |
| `services/instagram.py` | Instagram Graph API 캐러셀 게시 클라이언트 | requests, ENV: `IG_USER_ID`, `IG_ACCESS_TOKEN`, `PUBLIC_BASE_URL` |
| `web/insta_routes.py` | Flask 블루프린트 — 미리보기/API/관리자 페이지 | `models.InstaPost`, `services.instagram` |
| `templates/insta/card.html` | 1080x1080 슬라이드 디자인 (뉴스 보라, 툴팁 파랑) | Noto Sans KR (Google Fonts) |
| `templates/insta/admin_queue.html` | 관리자 큐 그리드 | `base.html` 상속 (CSS 변수 사용) |
| `templates/insta/admin_detail.html` | 단건 편집 페이지 | `base.html` 상속 |
| `data/tooltip_topics.yaml` | AI 툴 활용법 주제 풀 (8개 도구 × 35개 세부 주제) | pyyaml |
| `docs/instagram_setup.md` | Meta 앱 / 토큰 / ngrok 셋업 가이드 | — |
| `migrate_instagram.py` | SQLite `insta_posts` 테이블 생성 | sqlite3 |

## 데일리싱크에서 기존 파일에 추가됐던 변경분

cardnews_bot 에 옮길 때 비슷한 위치에 같은 내용을 넣어야 함.

### `models.py` — `InstaPost` 모델 추가

```python
class InstaPost(db.Model):
    __tablename__ = "insta_posts"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    content_type = db.Column(db.String(20), nullable=False, index=True)  # news | tooltip | promo
    source_cluster_id = db.Column(db.Integer, db.ForeignKey("clusters.id"), nullable=True, index=True)
    topic_key = db.Column(db.String(80), nullable=True)

    title = db.Column(db.String(300), nullable=False)
    slides = db.Column(db.JSON, default=list, nullable=False)
    caption = db.Column(db.Text, default="", nullable=False)
    hashtags = db.Column(db.JSON, default=list, nullable=False)

    image_paths = db.Column(db.JSON, default=list, nullable=False)
    rendered_at = db.Column(db.DateTime)

    status = db.Column(db.String(20), default="draft", nullable=False, index=True)
    scheduled_at = db.Column(db.DateTime, nullable=True, index=True)
    posted_at = db.Column(db.DateTime)
    approved = db.Column(db.Boolean, default=False, nullable=False)

    ig_media_id = db.Column(db.String(80))
    ig_permalink = db.Column(db.String(500))
    error = db.Column(db.Text)

    cluster = db.relationship("Cluster", foreign_keys=[source_cluster_id])
```

상태 흐름: `draft → ready → scheduled → posting → posted` (또는 `failed`).

### `config.py` — 추가된 환경변수 블록

```python
IG_USER_ID = os.getenv("IG_USER_ID", "")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "")
IG_FB_PAGE_ID = os.getenv("IG_FB_PAGE_ID", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
INSTA_REQUIRE_APPROVAL = os.getenv("INSTA_REQUIRE_APPROVAL", "true").lower() == "true"
INSTA_NEWS_POST_HOUR = int(os.getenv("INSTA_NEWS_POST_HOUR", "12"))
INSTA_TOOLTIP_POST_HOUR = int(os.getenv("INSTA_TOOLTIP_POST_HOUR", "19"))
INSTA_BRAND_NAME = os.getenv("INSTA_BRAND_NAME", "AI 데일리")
INSTA_BRAND_HANDLE = os.getenv("INSTA_BRAND_HANDLE", "")
```

### `app.py` — 블루프린트 등록

```python
from web.insta_routes import bp_insta
# ...
app.register_blueprint(bp_insta)
```

### `scheduler.py` — APScheduler 잡 등록

```python
from jobs.insta_pipeline import (
    job_insta_generate_daily,
    job_insta_render_pending,
    job_insta_publish_due,
)

# 매일 07:30 KST — 뉴스 + 툴팁 draft 생성
sched.add_job(_wrap(app, job_insta_generate_daily),
              CronTrigger(hour=7, minute=30, timezone=KST),
              id="insta_generate_daily", max_instances=1, coalesce=True)

# 매 30분 — draft 일괄 렌더
sched.add_job(_wrap(app, job_insta_render_pending),
              CronTrigger(minute="*/30", timezone=KST),
              id="insta_render_pending", max_instances=1, coalesce=True)

# 매 시간 :15 — 예약 시각 도래한 카드 게시
sched.add_job(_wrap(app, job_insta_publish_due),
              CronTrigger(minute=15, timezone=KST),
              id="insta_publish_due", max_instances=1, coalesce=True)
```

### `requirements.txt` — 추가

```
playwright>=1.50.0
```
설치 후: `python -m playwright install chromium` (브라우저 바이너리 ~170MB)

## cardnews_bot 으로 포팅할 때 어댑팅이 필요한 부분

1. **DB 계층**: 데일리싱크는 SQLAlchemy / SQLite. cardnews_bot 이 ORM 안 쓰면 `models.InstaPost` 를 raw SQL CRUD 로 다시 짜야 함. `migrate_instagram.py` 에 sqlite3 직접 사용 패턴 있음 — 참고.

2. **콘텐츠 소스 (뉴스)**: `jobs/insta_news.py` 는 데일리싱크 `Cluster` 테이블을 읽음. cardnews_bot 에서는:
   - 데일리싱크 DB(`F:\ai-news-digest\ai-news-digest\data\app.db`)를 read-only 로 직접 붙여 읽기, 또는
   - 데일리싱크 API 호출 (`http://localhost:5001/...`) 로 데이터 받기, 또는
   - 자체 뉴스 수집 로직 추가
   
   사용자는 처음에 "내 PC 데일리싱크에서 긁어오기" 라고 했으므로 첫 번째(read-only 외부 DB attach)가 가장 빠름.

3. **스케줄러**: cardnews_bot 이 APScheduler 안 쓰면 → cron + 외부 트리거, 또는 Telegram 봇 명령어로 수동 트리거.

4. **Flask 블루프린트**: cardnews_bot 의 `server.py` 가 단일 파일 라우트 구조면 `web/insta_routes.py` 의 라우트들을 그냥 그 안으로 옮기면 됨. 템플릿 경로(`templates/insta/...`)는 그대로 작동.

5. **Telegram 봇 통합 (추가 기회)**: cardnews_bot 은 Telegram 봇이 있으니, 카드 생성/승인/게시를 Telegram 메시지로 처리하면 관리자 페이지 안 띄워도 됨. 큐 알림 + 인라인 버튼(승인/거절/일정변경) 으로 즉시 액션. `jobs/insta_pipeline.job_insta_generate_daily` 끝에 봇 notify 한 줄 추가하는 식.

## 검증 완료된 동작 (데일리싱크 환경에서)

- DB 마이그레이션 OK
- 뉴스 카드 생성: Cluster #575 → 5장 슬라이드, Claude 응답 정상
- 툴팁 카드 생성: ChatGPT 주제 → 6장 슬라이드, Claude 응답 정상
- Playwright 렌더링: 11장 PNG (~450-490KB) 1초/슬라이드
- HTML 슬라이드 렌더 / Flask 라우트 모두 200 OK
- Instagram Graph API 게시는 **미테스트** (사용자 .env 미설정)

## 환경 의존성 메모

- Python 3.14 (시스템 글로벌, venv 아님 — 데일리싱크는 venv/.venv 둘 다 비어있는 잔재)
- Playwright + Chromium 이 시스템 Python 에 설치돼 있음 (`C:\Users\goldm\AppData\Local\ms-playwright\chromium_headless_shell-1223`)
- 정상 작동 확인된 라이브러리 버전: flask 3.1.3, anthropic 0.100.0, sqlalchemy 2.0.49, pyyaml 6.0.3

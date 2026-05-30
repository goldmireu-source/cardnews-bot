"""APScheduler 가동 — server.py 가 부팅 시 init_scheduler() 호출.

스케줄(KST):
  - 매 15분    : auto_jobs.job_publish_due     (예약된 세션 자동 발행)
  - 매일 03:00 : auto_jobs.job_refresh_tokens  (TikTok 24h 만료 대응 — Threads/Meta 도 같이)

note: 자동 카드 생성(job_generate_daily) 은 cron 등록 안 됨 (사용자 요청).
      함수 자체와 수동 트리거 매핑은 남아있어 `/api/auto/scheduler/trigger/generate_daily`
      로 수동 실행은 여전히 가능.

환경변수:
  AUTO_SCHEDULER       — 'false' 로 두면 스케줄러 미가동 (기본 'true')
  PUBLISH_CHECK_MIN    — 발행 체크 분 간격 (기본 15)
  TOKEN_REFRESH_HOUR   — 토큰 갱신 시각 (기본 3)
"""
import logging
import os
from datetime import timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

_scheduler: BackgroundScheduler | None = None


def init_scheduler() -> BackgroundScheduler | None:
    """전역 스케줄러를 생성하고 잡 등록. 멱등."""
    global _scheduler

    if os.getenv("AUTO_SCHEDULER", "true").lower() != "true":
        logger.info("AUTO_SCHEDULER=false — 스케줄러 미가동")
        return None

    if _scheduler is not None:
        logger.info("스케줄러 이미 가동 중")
        return _scheduler

    from auto_jobs import job_publish_due, job_refresh_tokens

    sched = BackgroundScheduler(timezone=KST)

    pub_min_interval = int(os.getenv("PUBLISH_CHECK_MIN", "15"))
    tok_hour = int(os.getenv("TOKEN_REFRESH_HOUR", "3"))

    # 발행 큐 체크
    sched.add_job(
        job_publish_due,
        CronTrigger(minute=f"*/{pub_min_interval}", timezone=KST),
        id="publish_due",
        name=f"예약 발행 체크 ({pub_min_interval}분)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 토큰 갱신 (매일 — TikTok access_token 24h 만료 대응)
    sched.add_job(
        job_refresh_tokens,
        CronTrigger(hour=tok_hour, minute=0, timezone=KST),
        id="refresh_tokens",
        name=f"장기 토큰 갱신 (매일 {tok_hour:02d}:00 KST)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    sched.start()
    _scheduler = sched

    logger.info("스케줄러 시작 — 등록된 잡:")
    for job in sched.get_jobs():
        logger.info(f"  - {job.id} ({job.name}): 다음 실행 {job.next_run_time}")
    return sched


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler


def trigger_job_now(job_id: str) -> bool:
    """잡을 즉시 한 번 실행 (관리자 수동 트리거)."""
    from datetime import datetime
    from auto_jobs import job_generate_daily, job_publish_due, job_refresh_tokens

    mapping = {
        "generate_daily": job_generate_daily,
        "publish_due": job_publish_due,
        "refresh_tokens": job_refresh_tokens,
    }
    fn = mapping.get(job_id)
    if not fn:
        return False

    sched = get_scheduler()
    if sched is None:
        # 스케줄러 미가동 시에는 직접 실행
        fn()
        return True

    sched.add_job(
        fn,
        "date",
        run_date=datetime.now(KST),
        id=f"manual_{job_id}_{int(datetime.now().timestamp())}",
        replace_existing=False,
    )
    return True

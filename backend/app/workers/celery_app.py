from celery import Celery

from app.core.config import settings

celery_app = Celery("docintel", broker=settings.redis_url, backend=settings.redis_url,
                    include=["app.workers.tasks"])
celery_app.conf.update(
    task_acks_late=True,  # a task is only acknowledged when finished, so a crashed worker's job is redelivered
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # long OCR jobs: don't hoard tasks
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,  # job state lives in the database; without this every upload also waits on a Redis result channel
    broker_connection_timeout=2,  # with Redis down, an upload fails in about a second instead of hanging
    task_publish_retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5},
    # With Redis, a job whose worker died is handed out again only after this long. It must be longer than the
    # slowest job (a 1000-page scan), or a healthy worker's job would be started a second time.
    broker_transport_options={"visibility_timeout": settings.celery_visibility_timeout_seconds},
)


@celery_app.task(name="ping")
def ping() -> str:
    return "pong"

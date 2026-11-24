from celery import Celery

from app.core.config import settings

celery_app = Celery("docintel", broker=settings.redis_url, backend=settings.redis_url,
                    include=["app.workers.tasks"])
celery_app.conf.update(
    task_acks_late=True,  # a task is only acknowledged when finished, so a crashed worker's job is redelivered
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # long OCR jobs: don't hoard tasks
    broker_connection_retry_on_startup=True,
    task_track_started=True,
    result_expires=3600,
)


@celery_app.task(name="ping")
def ping() -> str:
    return "pong"

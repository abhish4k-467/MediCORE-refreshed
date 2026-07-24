from celery import Celery

from backend.app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "supplier_intelligence",
    broker=settings.queue_url,
    backend=settings.queue_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    broker_connection_timeout=3,
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        "socket_connect_timeout": 3,
        "socket_timeout": 10,
    },
    beat_schedule={
        "poll-inbox-every-3-minutes": {
            "task": "backend.app.tasks.poll_inbox",
            "schedule": 180.0,
        }
    },
)

"""
Celery application configuration.

Queues:
  orders   – order import and confirmation tasks
  tracking – shipping/tracking sync tasks
  inventory – stock sync tasks

Beat schedule:
  import_orders   → every 300s  (5 min)
  confirm_orders  → every 360s  (6 min, after import)
  sync_tracking   → every 900s  (15 min)
  sync_inventory  → every 3600s (1 hour)
"""
from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "connector",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.sync_tasks"],
)

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Berlin",
    enable_utc=True,

    # Routing: separate queues for orders vs tracking
    task_routes={
        "app.tasks.sync_tasks.task_import_orders": {"queue": "orders"},
        "app.tasks.sync_tasks.task_confirm_orders": {"queue": "orders"},
        "app.tasks.sync_tasks.task_sync_tracking": {"queue": "tracking"},
        "app.tasks.sync_tasks.task_sync_inventory": {"queue": "inventory"},
    },

    # Retry defaults (per-task overrides in sync_tasks.py)
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Beat schedule
    beat_schedule={
        "import-orders-every-5min": {
            "task": "app.tasks.sync_tasks.task_import_orders",
            "schedule": settings.order_poll_interval,
            "options": {"queue": "orders"},
        },
        "confirm-orders-every-6min": {
            "task": "app.tasks.sync_tasks.task_confirm_orders",
            "schedule": settings.order_poll_interval + 60,
            "options": {"queue": "orders"},
        },
        "sync-tracking-every-15min": {
            "task": "app.tasks.sync_tasks.task_sync_tracking",
            "schedule": settings.tracking_poll_interval,
            "options": {"queue": "tracking"},
        },
        "sync-inventory-every-hour": {
            "task": "app.tasks.sync_tasks.task_sync_inventory",
            "schedule": 3600,
            "options": {"queue": "inventory"},
        },
        # Monthly quota reset on day 1 at 00:05 UTC
        "monthly-quota-reset": {
            "task": "app.tasks.sync_tasks.task_monthly_reset",
            "schedule": crontab(day_of_month="1", hour="0", minute="5"),
            "options": {"queue": "orders"},
        },
    },
)

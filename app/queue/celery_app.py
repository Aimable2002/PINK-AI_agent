from celery import Celery

from app.config import REDIS_URL

celery_app = Celery(
    "agent_backend",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["app.queue.tasks"],
)

celery_app.conf.update(
    task_routes={
        "app.queue.tasks.run_paid_job": {"queue": "paid_priority"},
        "app.queue.tasks.run_free_job": {"queue": "free_standard"},
    },
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=120,
    task_soft_time_limit=100,
    result_expires=3600,
)

# Production worker startup (separate pools so a paid surge can't starve
# free-tier throughput, and vice versa):
#   celery -A app.queue.celery_app worker -Q paid_priority --concurrency=6 -n paid@%h
#   celery -A app.queue.celery_app worker -Q free_standard --concurrency=2 -n free@%h

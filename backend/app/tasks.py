"""Celery dispatch, database outbox recovery, and single-process local mode."""
from datetime import datetime, timedelta, timezone
from celery import Celery
from sqlalchemy import select
from .config import settings
from .db import SessionLocal
from .models import ImportJob

celery_app = Celery('zhixu', broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1, task_time_limit=2400, task_soft_time_limit=2300, beat_schedule={'recover-import-jobs': {'task': 'zhixu.recover', 'schedule': 60.0}})


@celery_app.task(name='zhixu.import', bind=True, max_retries=2)
def import_document(self, job_id):
    from .ingestion import process_job
    process_job(job_id)


def dispatch_job(job_id: str, background_tasks=None):
    if settings.task_mode == 'celery':
        try:
            import_document.apply_async(args=[job_id], task_id=job_id)
        except Exception:
            # The committed queued record is the outbox; recovery dispatches it later.
            return False
    elif background_tasks is not None:
        from .ingestion import process_job
        background_tasks.add_task(process_job, job_id)
    else:
        from .ingestion import process_job
        process_job(job_id)
    return True


@celery_app.task(name='zhixu.recover')
def recover_jobs():
    stale = datetime.now(timezone.utc) - timedelta(minutes=40)
    with SessionLocal() as db:
        jobs = db.scalars(select(ImportJob).where((ImportJob.status == 'queued') | ((ImportJob.status == 'processing') & (ImportJob.updated_at < stale))).limit(200)).all()
        ids = []
        for job in jobs:
            if job.status == 'processing':
                job.status = 'queued'
                job.options = {**job.options, 'recovered': True}
            ids.append(job.id)
        db.commit()
    for job_id in ids:
        dispatch_job(job_id)
    cleaned = retry_cleanup()
    return {'dispatched': len(ids), 'cleaned': cleaned}


def recover_local_jobs():
    """Run once at local API startup; celery deployments use the beat schedule."""
    if settings.task_mode != 'eager':
        return
    with SessionLocal() as db:
        jobs = db.scalars(select(ImportJob).where(ImportJob.status.in_(['queued', 'processing']))).all()
        ids = []
        for job in jobs:
            job.status = 'queued'
            ids.append(job.id)
        db.commit()
    for job_id in ids:
        dispatch_job(job_id)
    retry_cleanup()


def retry_cleanup():
    """Retry bounded best-effort cleanup using the published document version."""
    from .models import DocumentRecord
    from .retrieval import cleanup_document
    count = 0
    with SessionLocal() as db:
        jobs = db.scalars(select(ImportJob).where(ImportJob.options['cleanupPending'].as_boolean().is_(True)).order_by(ImportJob.updated_at).limit(100)).all()
        for job in jobs:
            if not job.options.get('cleanupPending'):
                continue
            document = db.get(DocumentRecord, job.document_id)
            if not document:
                continue
            try:
                cleanup_document(document.id, document.kb_id, keep_version=document.active_version if document.status != 'deleted' else None)
                job.options = {**job.options, 'cleanupPending': False}
                count += 1
            except Exception:
                continue
        db.commit()
    return count

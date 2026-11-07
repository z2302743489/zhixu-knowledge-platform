"""Versioned, restartable document imports backed by database job records."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .models import ChunkRecord, DocumentRecord, ImportJob, KnowledgeBase, LogEntry, ModelConfig, Setting
from .parsers import parse_file, split_documents


def now():
    return datetime.now(timezone.utc)


def ocr_configuration(db) -> dict:
    from .security import decrypt
    row = db.get(Setting, 'ocr')
    if not row:
        return {}
    value = row.value
    return {'region': value.get('region', 'cn-hangzhou'), 'accessKeyId': decrypt(value.get('encryptedAccessKeyId', '')), 'accessKeySecret': decrypt(value.get('encryptedAccessKeySecret', ''))}


def make_job(db, document: DocumentRecord, options: dict, *, build_only: bool = False, version: str | None = None) -> ImportJob:
    pending = db.scalar(select(ImportJob).where(ImportJob.document_id == document.id, ImportJob.status.in_(['queued', 'processing'])))
    if pending:
        return pending
    opts = {**options, 'version': version or str(uuid4()), 'buildOnly': build_only}
    job = ImportJob(id=str(uuid4()), document_id=document.id, status='queued', stage='queued', progress=0, options=opts)
    document.error = None
    if not document.active_version:
        document.status = 'processing'
    db.add(job)
    db.flush()
    return job


def update_job(db, job, *, status=None, stage=None, progress=None, options=None):
    if status is not None:
        job.status = status
    if stage is not None:
        job.stage = stage
    if progress is not None:
        job.progress = progress
    if options is not None:
        job.options = options
    job.updated_at = now()
    db.commit()


def process_job(job_id: str):
    from .observability import trace_context
    with SessionLocal() as session:
        row = session.get(ImportJob, job_id)
        trace = {'trace_id': job_id, 'owner': row.options.get('owner', '') if row else '', 'question': '知识文档导入与向量化'}
    token = trace_context.set(trace)
    try:
        _process_job(job_id)
    finally:
        trace_context.reset(token)


def _process_job(job_id: str):
    started = time.monotonic()
    with SessionLocal() as db:
        claimed = db.execute(update(ImportJob).where(ImportJob.id == job_id, ImportJob.status == 'queued').values(status='processing', stage='parsing', progress=5, updated_at=now()))
        if claimed.rowcount != 1:
            db.rollback()
            return
        db.commit()
        job = db.get(ImportJob, job_id)
        document = db.get(DocumentRecord, job.document_id)
        if not document or document.status in {'deleted', 'disabled'}:
            job.status, job.stage, job.error = 'failed', 'failed', '文档已停用或删除'
            db.commit()
            return
        kb = db.get(KnowledgeBase, document.kb_id)
        if not kb:
            return
        update_job(db, job, status='processing', stage='parsing', progress=5)
        try:
            version = job.options['version']
            if not job.options.get('buildOnly'):
                def persist_remote(state):
                    update_job(db, job, options={**job.options, 'remoteJob': state}, stage='cloud_parsing', progress=20)
                source_file = job.options.get('sourceFile') or {'path': document.path, 'name': document.name}
                blocks = parse_file(Path(source_file['path']), source_file['name'], cloud_parser=job.options.get('cloudParser', True), ocr_config=ocr_configuration(db), remote_jobs=job.options.get('remoteJob', {}), save_remote=persist_remote)
                update_job(db, job, stage='splitting', progress=40)
                documents = split_documents(blocks, int(job.options.get('chunkSize', 800)), int(job.options.get('overlap', 100)))
                # Stable IDs within a version make safe retries idempotent.
                from uuid import UUID, uuid5
                db.execute(delete(ChunkRecord).where(ChunkRecord.document_id == document.id, ChunkRecord.version == version))
                for ordinal, block in enumerate(documents):
                    location = block.metadata.get('location', '')
                    if block.metadata.get('start_index', 0):
                        location += f' / 块内字符偏移 {block.metadata["start_index"]}'
                    db.add(ChunkRecord(id=str(uuid5(UUID(version), str(ordinal))), document_id=document.id, version=version, text=block.page_content, location=location, enabled=True, ordinal=ordinal))
                db.commit()
                if not job.options.get('autoBuild', True):
                    if not document.active_version:
                        document.status = 'preview'
                    update_job(db, job, status='preview', stage='preview', progress=60)
                    return
            chunks = db.scalars(select(ChunkRecord).where(ChunkRecord.document_id == document.id, ChunkRecord.version == version).order_by(ChunkRecord.ordinal)).all()
            if not chunks:
                raise ValueError('没有可构建的解析片段，请先重新解析')
            model = db.get(ModelConfig, kb.model_id)
            if not model:
                raise ValueError('知识库未绑定 Embedding 模型')
            update_job(db, job, stage='embedding', progress=65)
            from .retrieval import cleanup_document, index_chunks
            def progress(done, total):
                update_job(db, job, stage='embedding', progress=65 + int(30 * done / total))
            index_chunks(db, kb, chunks, model, progress=progress)
            # Re-read authoritative state after network calls before publishing.
            db.refresh(document)
            if document.status in {'disabled', 'deleted'}:
                raise ValueError('文档在构建期间被停用或删除，索引未发布')
            manifest = db.get(Setting, 'document_files:' + document.id)
            versions = dict((manifest.value if manifest else {}).get('versions', {}))
            if document.active_version and document.active_version not in versions:
                versions[document.active_version] = {'path': document.path, 'name': document.name, 'kind': document.kind, 'sha256': document.sha256, 'sizeBytes': document.size_bytes}
            source = job.options.get('sourceFile') or {'path': document.path, 'name': document.name, 'kind': document.kind, 'sha256': document.sha256, 'sizeBytes': document.size_bytes}
            versions[version] = source
            if manifest:
                manifest.value = {'versions': versions}
            else:
                db.add(Setting(key='document_files:' + document.id, value={'versions': versions}))
            document.path, document.name, document.kind = source['path'], source['name'], source['kind']
            document.sha256, document.size_bytes = source['sha256'], source['sizeBytes']
            document.active_version = version
            document.status, document.error = 'ready', None
            job.status, job.stage, job.progress, job.updated_at = 'completed', 'completed', 100, now()
            db.add(LogEntry(kind='导入', question=f'导入并构建：{document.name}', model=model.name, tokens=None, duration=round(time.monotonic() - started, 3), status='success', owner=job.options.get('owner', ''), details={'jobId': job.id, 'trace_id': job.id, 'documentId': document.id, 'version': version, 'chunks': len(chunks), 'usageSource': 'unavailable', 'cloudJob': job.options.get('remoteJob', {}).get('id')}))
            db.commit()
            try:
                cleanup_document(document.id, kb.id, keep_version=version)
            except Exception:
                # SQL active-version validation protects retrieval until cleanup succeeds.
                job.options = {**job.options, 'cleanupPending': True}
                db.commit()
        except Exception as exc:
            db.rollback()
            job = db.get(ImportJob, job_id)
            document = db.get(DocumentRecord, job.document_id) if job else None
            if not job or not document:
                return
            from .parsers import ParseError
            from .providers import ProviderError
            error = str(exc)[:500] if isinstance(exc, (ParseError, ProviderError)) else '解析或索引失败，请检查文件、模型/云端配置及服务运行状态'
            if type(exc) is ValueError:
                error = str(exc)[:500]
            if isinstance(exc, IntegrityError):
                error = '此知识库已存在相同内容的文件，新版本未发布；请使用已有文档'
                job.options = {**job.options, 'cleanupPending': True}
            job.status, job.stage, job.error, job.updated_at = 'failed', 'failed', error, now()
            document.error = error
            if not document.active_version and document.status not in {'disabled', 'deleted'}:
                document.status = 'failed'
            db.add(LogEntry(kind='导入', question=f'导入失败：{document.name}', model='文档解析与索引', tokens=None, duration=round(time.monotonic() - started, 3), status='failed', owner=job.options.get('owner', ''), details={'jobId': job.id, 'trace_id': job.id, 'error': error, 'oldVersionRetained': bool(document.active_version)}))
            db.commit()

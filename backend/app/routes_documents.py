"""Document upload, parsed previews, versioned builds and retrieval diagnostics."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import get_db
from .ingestion import make_job
from .models import ChunkRecord, DocumentRecord, ImportJob, KnowledgeBase, Assistant, Setting
from .parsers import MAX_FILE_BYTES, MIME_TYPES, ParseError, validate_file
from .security import owner, require_manager, role
from .serialization import chunk_dto, document_dto
from .tasks import dispatch_job

router = APIRouter(prefix='/api')


def _document(db, document_id):
    doc = db.get(DocumentRecord, document_id)
    if not doc or doc.status == 'deleted':
        raise HTTPException(404, '文档不存在')
    return doc


def _job_dto(job):
    return {'id': job.id, 'documentId': job.document_id, 'status': job.status, 'stage': job.stage, 'progress': job.progress, 'error': job.error, 'createdAt': job.created_at.isoformat(), 'updatedAt': job.updated_at.isoformat(), 'version': job.options.get('version')}


def _options(raw):
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(422, '导入参数必须为 JSON')
    size, overlap = value.get('chunkSize', 800), value.get('overlap', 100)
    if type(size) is not int or type(overlap) is not int or not 200 <= size <= 4000 or not 0 <= overlap < size:
        raise HTTPException(422, '分块字符长度需为 200–4000，重叠需小于分块长度')
    return {'chunkSize': size, 'overlap': overlap, 'cloudParser': bool(value.get('cloudParser', True)), 'autoBuild': bool(value.get('autoBuild', True))}


@router.post('/kbs/{kb_id}/documents', dependencies=[Depends(require_manager)])
async def upload_documents(kb_id: str, request: Request, background_tasks: BackgroundTasks, files: list[UploadFile] = File(...), options: str = Form('{}'), db=Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(404, '知识库不存在')
    if not kb.enabled:
        raise HTTPException(409, '请先启用知识库')
    if len(files) > 30:
        raise HTTPException(422, '单次最多导入 30 份文件')
    opts = _options(options)
    storage = (settings.data_dir / 'uploads').resolve()
    storage.mkdir(parents=True, exist_ok=True)
    documents, jobs, errors = [], [], []
    batch_bytes = 0
    batch_limit = min(MAX_FILE_BYTES, settings.max_upload_mb * 1024 * 1024)
    for upload in files:
        clean_name = Path((upload.filename or 'file').replace('\\', '/')).name[:250]
        extension = Path(clean_name).suffix.lower()
        target = storage / (str(uuid4()) + extension)
        size, digest = 0, hashlib.sha256()
        try:
            with target.open('xb') as output:
                while content := await upload.read(1024 * 1024):
                    size += len(content)
                    batch_bytes += len(content)
                    if size > batch_limit:
                        raise ParseError('文件超过上传大小限制')
                    if batch_bytes > batch_limit:
                        raise ParseError('单次上传总量超过 50 MB（或部署上限），请分批导入；本批此前的有效文件仍会保留')
                    output.write(content)
                    digest.update(content)
            kind = validate_file(target, clean_name)
            existing = db.scalar(select(DocumentRecord).where(DocumentRecord.kb_id == kb_id, DocumentRecord.sha256 == digest.hexdigest(), DocumentRecord.status != 'deleted'))
            if existing:
                target.unlink(missing_ok=True)
                documents.append({**document_dto(existing, db), 'duplicate': True})
                pending = db.scalar(select(ImportJob).where(ImportJob.document_id == existing.id).order_by(ImportJob.created_at.desc()))
                if pending:
                    jobs.append(_job_dto(pending))
                continue
            doc = DocumentRecord(id=str(uuid4()), kb_id=kb_id, name=clean_name, kind=kind, size_bytes=size, path=str(target), sha256=digest.hexdigest(), status='processing')
            db.add(doc)
            db.flush()
            job = make_job(db, doc, {**opts, 'owner': owner(request)})
            db.commit()
            documents.append(document_dto(doc, db))
            jobs.append(_job_dto(job))
            dispatch_job(job.id, background_tasks)
        except IntegrityError:
            # A concurrent upload may commit the same digest after our initial
            # lookup. The database index selects the winner across API workers.
            db.rollback()
            target.unlink(missing_ok=True)
            existing = db.scalar(select(DocumentRecord).where(DocumentRecord.kb_id == kb_id, DocumentRecord.sha256 == digest.hexdigest(), DocumentRecord.status != 'deleted'))
            if existing:
                documents.append({**document_dto(existing, db), 'duplicate': True})
                pending = db.scalar(select(ImportJob).where(ImportJob.document_id == existing.id).order_by(ImportJob.created_at.desc()))
                if pending:
                    jobs.append(_job_dto(pending))
            else:
                errors.append({'name': clean_name, 'error': '文件记录发生并发冲突，请重试'})
        except (ParseError, OSError) as exc:
            db.rollback()
            target.unlink(missing_ok=True)
            errors.append({'name': clean_name, 'error': str(exc) if isinstance(exc, ParseError) else '文件保存失败'})
        finally:
            await upload.close()
    if not documents:
        raise HTTPException(422, errors[0]['error'] if errors else '没有可导入文件')
    return {'documents': documents, 'jobs': jobs, 'errors': errors}


@router.get('/kbs/{kb_id}/documents', dependencies=[Depends(require_manager)])
def list_documents(kb_id: str, page: int = Query(1, ge=1), pageSize: int = Query(50, ge=1, le=100), query: str = Query('', max_length=200), db=Depends(get_db)):
    conditions = [DocumentRecord.kb_id == kb_id, DocumentRecord.status != 'deleted']
    if query.strip():
        conditions.append(DocumentRecord.name.ilike('%' + query.strip().replace('%', '\\%').replace('_', '\\_') + '%', escape='\\'))
    total = db.scalar(select(func.count()).select_from(DocumentRecord).where(*conditions))
    docs = db.scalars(select(DocumentRecord).where(*conditions).order_by(DocumentRecord.created_at.desc()).offset((page - 1) * pageSize).limit(pageSize))
    return {'items': [document_dto(d, db) for d in docs], 'total': total, 'page': page}


@router.get('/jobs/{job_id}', dependencies=[Depends(require_manager)])
def get_job(job_id: str, db=Depends(get_db)):
    job = db.get(ImportJob, job_id)
    if not job:
        raise HTTPException(404, '任务不存在')
    return _job_dto(job)


@router.get('/documents/{document_id}/jobs', dependencies=[Depends(require_manager)])
def document_jobs(document_id: str, db=Depends(get_db)):
    _document(db, document_id)
    jobs = db.scalars(select(ImportJob).where(ImportJob.document_id == document_id).order_by(ImportJob.created_at.desc()).limit(20))
    return {'items': [_job_dto(j) for j in jobs]}


@router.post('/documents/{document_id}/build', dependencies=[Depends(require_manager)])
def build_document(document_id: str, request: Request, background_tasks: BackgroundTasks, db=Depends(get_db)):
    doc = _document(db, document_id)
    if doc.status == 'disabled':
        raise HTTPException(409, '请先启用文档')
    preview = db.scalar(select(ImportJob).where(ImportJob.document_id == doc.id, ImportJob.status.in_(['preview', 'failed'])).order_by(ImportJob.created_at.desc()))
    version = preview.options.get('version') if preview else doc.active_version
    if not version or not db.scalar(select(ChunkRecord.id).where(ChunkRecord.document_id == doc.id, ChunkRecord.version == version)):
        raise HTTPException(409, '请先解析文档并检查预览')
    options = {**(preview.options if preview else {}), 'owner': owner(request), 'autoBuild': True}
    job = make_job(db, doc, options, build_only=True, version=version)
    db.commit()
    dispatch_job(job.id, background_tasks)
    return {'jobId': job.id}


@router.post('/documents/{document_id}/rebuild', dependencies=[Depends(require_manager)])
def rebuild_document(document_id: str, request: Request, background_tasks: BackgroundTasks, db=Depends(get_db)):
    doc = _document(db, document_id)
    if doc.status == 'disabled':
        raise HTTPException(409, '请先启用文档')
    previous = db.scalar(select(ImportJob).where(ImportJob.document_id == doc.id).order_by(ImportJob.created_at.desc()))
    options = {**(previous.options if previous else {}), 'owner': owner(request), 'autoBuild': True}
    if previous and previous.status != 'failed':
        options.pop('remoteJob', None)
    job = make_job(db, doc, options)
    db.commit()
    dispatch_job(job.id, background_tasks)
    return {'jobId': job.id}


@router.get('/documents/{document_id}/chunks', dependencies=[Depends(require_manager)])
def list_chunks(document_id: str, preview: bool = False, page: int = Query(1, ge=1), pageSize: int = Query(100, ge=1, le=500), db=Depends(get_db)):
    doc = _document(db, document_id)
    version = doc.active_version
    if preview or not version:
        latest = db.scalar(select(ImportJob).where(ImportJob.document_id == doc.id).order_by(ImportJob.created_at.desc()))
        version = latest.options.get('version', '') if latest else version
    cond = (ChunkRecord.document_id == doc.id, ChunkRecord.version == version)
    count = db.scalar(select(func.count()).select_from(ChunkRecord).where(*cond)) or 0
    items = db.scalars(select(ChunkRecord).where(*cond).order_by(ChunkRecord.ordinal).offset((page - 1) * pageSize).limit(pageSize))
    return {'items': [chunk_dto(c) for c in items], 'total': count, 'version': version, 'page': page}


@router.patch('/chunks/{chunk_id}', dependencies=[Depends(require_manager)])
def patch_chunk(chunk_id: str, request: Request, background_tasks: BackgroundTasks, value: dict = Body(...), db=Depends(get_db)):
    chunk = db.get(ChunkRecord, chunk_id)
    if not chunk:
        raise HTTPException(404, '片段不存在')
    doc = _document(db, chunk.document_id)
    if set(value) - {'text', 'enabled'}:
        raise HTTPException(422, '仅支持修改文字与启用状态')
    if 'text' in value and (not isinstance(value['text'], str) or not value['text'].strip() or len(value['text']) > 30_000):
        raise HTTPException(422, '片段文字需为 1–30000 字符')
    if 'enabled' in value and not isinstance(value['enabled'], bool):
        raise HTTPException(422, '启用状态必须为布尔值')
    if 'text' not in value or value['text'] == chunk.text or chunk.version != doc.active_version:
        if 'text' in value:
            chunk.text = value['text']
        if 'enabled' in value:
            chunk.enabled = value['enabled']
        db.commit()
        return chunk_dto(chunk)
    if db.scalar(select(ImportJob.id).where(ImportJob.document_id == doc.id, ImportJob.status.in_(['queued', 'processing']))):
        raise HTTPException(409, '文档正在构建，请完成后编辑')
    # Clone before re-embedding so a failed edit never changes the published version.
    new_version = str(uuid4())
    old_chunks = db.scalars(select(ChunkRecord).where(ChunkRecord.document_id == doc.id, ChunkRecord.version == doc.active_version).order_by(ChunkRecord.ordinal)).all()
    replacement = None
    for source in old_chunks:
        changed = source.id == chunk.id
        copy = ChunkRecord(id=str(uuid5(UUID(new_version), str(source.ordinal))), document_id=doc.id, version=new_version, ordinal=source.ordinal, location=source.location, text=value.get('text', source.text) if changed else source.text, enabled=value.get('enabled', source.enabled) if changed else source.enabled)
        db.add(copy)
        if changed:
            replacement = copy
    db.flush()
    job = make_job(db, doc, {'owner': owner(request), 'autoBuild': True}, build_only=True, version=new_version)
    db.commit()
    dispatch_job(job.id, background_tasks)
    return {**chunk_dto(replacement), 'jobId': job.id, 'pendingReindex': True}


@router.patch('/documents/{document_id}', dependencies=[Depends(require_manager)])
def patch_document(document_id: str, value: dict = Body(...), db=Depends(get_db)):
    doc = _document(db, document_id)
    if set(value) != {'enabled'} or not isinstance(value['enabled'], bool):
        raise HTTPException(422, '仅支持布尔 enabled 参数')
    if value['enabled']:
        doc.status = 'ready' if doc.active_version else 'preview' if db.scalar(select(ChunkRecord.id).where(ChunkRecord.document_id == doc.id)) else 'failed'
    else:
        doc.status = 'disabled'
    db.commit()
    return document_dto(doc, db)


@router.delete('/documents/{document_id}', dependencies=[Depends(require_manager)])
def delete_document(document_id: str, db=Depends(get_db)):
    doc = _document(db, document_id)
    doc.status = 'deleted'
    db.commit()
    try:
        from .retrieval import cleanup_document
        cleanup_document(doc.id, doc.kb_id)
    except Exception:
        job = db.scalar(select(ImportJob).where(ImportJob.document_id == doc.id).order_by(ImportJob.created_at.desc()))
        if job:
            job.options = {**job.options, 'cleanupPending': True}
            db.commit()
    return {'ok': True}


@router.post('/documents/{document_id}/versions', dependencies=[Depends(require_manager)])
async def upload_version(document_id: str, request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...), options: str = Form('{}'), db=Depends(get_db)):
    doc = _document(db, document_id)
    if doc.status == 'disabled':
        raise HTTPException(409, '请先启用文档')
    if db.scalar(select(ImportJob.id).where(ImportJob.document_id == doc.id, ImportJob.status.in_(['queued', 'processing']))):
        raise HTTPException(409, '文档正在构建，请完成后更新版本')
    opts = _options(options)
    clean_name = Path((file.filename or doc.name).replace('\\', '/')).name[:250]
    storage = (settings.data_dir / 'uploads').resolve()
    storage.mkdir(parents=True, exist_ok=True)
    path = storage / (str(uuid4()) + Path(clean_name).suffix.lower())
    size, digest = 0, hashlib.sha256()
    try:
        with path.open('xb') as output:
            while content := await file.read(1024 * 1024):
                size += len(content)
                if size > min(MAX_FILE_BYTES, settings.max_upload_mb * 1024 * 1024):
                    raise ParseError('文件超过上传大小限制')
                output.write(content)
                digest.update(content)
        kind = validate_file(path, clean_name)
        if digest.hexdigest() == doc.sha256:
            path.unlink(missing_ok=True)
            return {'document': document_dto(doc, db), 'duplicate': True, 'jobId': None}
        duplicate = db.scalar(select(DocumentRecord.id).where(DocumentRecord.kb_id == doc.kb_id, DocumentRecord.id != doc.id, DocumentRecord.sha256 == digest.hexdigest(), DocumentRecord.status != 'deleted'))
        if duplicate:
            path.unlink(missing_ok=True)
            raise HTTPException(409, '此知识库已存在相同内容的文件，请使用已有文档')
        source = {'path': str(path), 'name': clean_name, 'kind': kind, 'sha256': digest.hexdigest(), 'sizeBytes': size}
        job = make_job(db, doc, {**opts, 'sourceFile': source, 'owner': owner(request)})
        db.commit()
        dispatch_job(job.id, background_tasks)
        return {'document': document_dto(doc, db), 'jobId': job.id}
    except (ParseError, OSError) as exc:
        db.rollback()
        path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc) if isinstance(exc, ParseError) else '文件保存失败') from exc
    finally:
        await file.close()


@router.get('/documents/{document_id}/file')
def original_file(document_id: str, version: str | None = None, current_role=Depends(role), db=Depends(get_db)):
    doc = _document(db, document_id)
    if current_role == 'user':
        assistants = db.scalars(select(Assistant).where(Assistant.published.is_(True))).all()
        if doc.status != 'ready' or not any(doc.kb_id in a.kb_ids for a in assistants):
            raise HTTPException(403, '该文件未公开给已发布助手')
    source = {'path': doc.path, 'name': doc.name, 'kind': doc.kind}
    if version and version != doc.active_version:
        manifest = db.get(Setting, 'document_files:' + doc.id)
        source = (manifest.value if manifest else {}).get('versions', {}).get(version)
        if not source:
            raise HTTPException(404, '该文件版本不存在或尚未发布')
    path = Path(source['path']).resolve()
    root = (settings.data_dir / 'uploads').resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, '原文件不存在')
    return FileResponse(path, media_type=MIME_TYPES.get(source['kind'], 'application/octet-stream'), filename=source['name'], content_disposition_type='inline' if source['kind'] in {'pdf', 'png', 'jpg', 'jpeg', 'webp'} else 'attachment', headers={'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; sandbox"})


@router.post('/kbs/{kb_id}/search', dependencies=[Depends(require_manager)])
def test_search(kb_id: str, value: dict = Body(...), db=Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, '知识库不存在')
    question = value.get('question', '')
    if not isinstance(question, str) or not question.strip() or len(question) > 8000:
        raise HTTPException(422, '请输入 1–8000 字符问题')
    started = time.monotonic()
    from .retrieval import retrieve
    try:
        items = retrieve(db, question, [kb_id], mode=value.get('mode', 'hybrid'), rerank=bool(value.get('rerank', False)), top_k=int(value.get('topK', 5)))
        db.commit()
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(502, '检索服务调用失败，请检查模型、向量服务与网络配置') from exc
    return {'results': items, 'duration': round(time.monotonic() - started, 3)}

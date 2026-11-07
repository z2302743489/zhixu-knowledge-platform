from datetime import datetime, timezone
from sqlalchemy import select, func
from .models import ChunkRecord, DocumentRecord, ChatMessage, ImportJob


def iso(value):
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    return value


def model_dto(m):
    return {"id": m.id, "name": m.name, "provider": m.provider, "type": m.type,
            "modelId": m.model_id, "base": m.base, "dim": m.dimension, "enabled": m.enabled,
            "hasKey": bool(m.encrypted_key), "options": m.options or {}, "tested": bool((m.options or {}).get("tested"))}


def chunk_dto(c):
    return {"id": c.id, "text": c.text, "location": c.location, "enabled": c.enabled, "version": c.version}


def document_dto(d, db=None, include_chunks=True):
    chunks = []
    total = 0
    version = d.active_version
    if db is not None and not version:
        latest = db.scalar(select(ImportJob).where(ImportJob.document_id == d.id).order_by(ImportJob.created_at.desc()))
        version = latest.options.get("version") if latest else None
    if db is not None and version:
        cond = (ChunkRecord.document_id == d.id, ChunkRecord.version == version)
        total = db.scalar(select(func.count()).select_from(ChunkRecord).where(*cond)) or 0
        if include_chunks:
            chunks = [chunk_dto(c) for c in db.scalars(select(ChunkRecord).where(*cond).order_by(ChunkRecord.ordinal).limit(500))]
    return {"id": d.id, "name": d.name, "kind": d.kind, "status": d.status,
            "chunks": chunks, "totalChunks": total, "size": f"{d.size_bytes / 1024:.1f} KB",
            "sizeBytes": d.size_bytes, "date": iso(d.created_at), "error": d.error,
            "activeVersion": d.active_version}


def kb_dto(k, db=None, with_docs=False):
    docs, total = [], 0
    if db is not None:
        total = db.scalar(select(func.count()).select_from(DocumentRecord).where(DocumentRecord.kb_id == k.id, DocumentRecord.status != "deleted")) or 0
        if with_docs:
            # Bootstrap carries counts/metadata; chunk bodies are fetched through
            # their paginated endpoint when the document is opened.
            docs = [document_dto(d, db, False) for d in db.scalars(select(DocumentRecord).where(DocumentRecord.kb_id == k.id, DocumentRecord.status != "deleted").order_by(DocumentRecord.created_at.desc()).limit(100))]
    return {"id": k.id, "name": k.name, "description": k.description, "category": k.category,
            "color": k.color, "model": k.model_id, "enabled": k.enabled, "docs": docs, "totalDocs": total}


def assistant_dto(a):
    return {"id": a.id, "name": a.name, "description": a.description, "kbIds": a.kb_ids,
            "model": a.model_id, "prompt": a.prompt, "greeting": a.greeting,
            "rerank": a.rerank, "published": a.published, "options": a.options or {}}


def message_dto(m):
    return {"id": m.id, "role": m.role, "text": m.text, "refs": m.refs or [], "status": m.status,
            "feedback": m.feedback, "createdAt": iso(m.created_at)}


def session_dto(s, db):
    return {"id": s.id, "title": s.title, "assistantId": s.assistant_id,
            "messages": [message_dto(m) for m in db.scalars(select(ChatMessage).where(ChatMessage.session_id == s.id).order_by(ChatMessage.created_at).limit(200))]}


def log_dto(l):
    return {"id": l.id, "time": iso(l.time), "kind": l.kind, "question": l.question,
            "model": l.model, "tokens": l.tokens, "duration": l.duration, "status": l.status,
            "refs": l.refs or [], "details": l.details or {}}


# Friendly aliases for route modules.
doc_dto = document_dto

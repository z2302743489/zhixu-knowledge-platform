import asyncio
import time
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, func, delete
from sqlalchemy.orm import Session
from .db import get_db
from .models import ModelConfig, KnowledgeBase, DocumentRecord, ChunkRecord, ImportJob, Assistant, ChatSession, LogEntry, Setting
from .schemas import KBCreate, KBUpdate, ModelCreate, ModelUpdate, AssistantCreate, AssistantUpdate, OCRUpdate
from .security import encrypt, require_admin, require_manager, role, owner
from .serialization import model_dto, kb_dto, assistant_dto, session_dto, log_dto, document_dto
from .providers import chat_model, embedding_model, rerank_documents, ProviderError

router = APIRouter(prefix="/api", tags=["management"])


def get_or_404(db, cls, id):
    value = db.get(cls, id)
    if not value:
        raise HTTPException(404, "记录不存在")
    return value


def require_model(db, id, type):
    model = get_or_404(db, ModelConfig, id)
    if model.type != type or not model.enabled:
        raise HTTPException(422, f"请选择已启用的 {type} 模型")
    return model


def ocr_dto(db):
    setting = db.get(Setting, "ocr")
    v = setting.value if setting else {}
    return {"configured": bool(v.get("encryptedAccessKeyId") and v.get("encryptedAccessKeySecret")),
            "region": v.get("region", "cn-hangzhou"), "endpoint": v.get("endpoint", "docmind-api.cn-hangzhou.aliyuncs.com")}


@router.get("/bootstrap")
def bootstrap(request: Request, mode=Depends(role), db: Session = Depends(get_db)):
    kbs = [kb_dto(k, db, True) for k in db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.name).limit(200))] if mode != "user" else []
    assistants = db.scalars(select(Assistant).where(Assistant.published == True) if mode == "user" else select(Assistant)).all()
    sessions = db.scalars(select(ChatSession).where(ChatSession.owner == owner(request)).order_by(ChatSession.created_at.desc()).limit(50)).all()
    return {"kbs": kbs, "models": [model_dto(m) for m in db.scalars(select(ModelConfig))] if mode == "admin" else [{"id": m.id, "name": m.name, "provider": m.provider, "type": m.type, "modelId": "", "base": "", "dim": m.dimension, "enabled": m.enabled, "hasKey": bool(m.encrypted_key), "options": {}} for m in db.scalars(select(ModelConfig))] if mode == "manager" else [],
            "assistants": [assistant_dto(a) for a in assistants],
            "ready": {"llm": any(m.enabled and m.type == "LLM" and bool(m.encrypted_key) for m in db.scalars(select(ModelConfig))),
                      "embedding": any(m.enabled and m.type == "Embedding" and bool(m.encrypted_key) for m in db.scalars(select(ModelConfig))),
                      "ocr": ocr_dto(db)["configured"]},
            "sessions": [session_dto(s, db) for s in sessions],
            "logs": [log_dto(l) for l in db.scalars(select(LogEntry).order_by(LogEntry.time.desc()).limit(100))] if mode == "admin" else [],
            "ocr": ocr_dto(db) if mode == "admin" else {"configured": False}, "mode": "live",
            "limits": {"knowledgeBases": 200, "documentsPerKB": 100, "chunksPerDocument": 500, "sessions": 50, "messagesPerSession": 200},
            "role": mode}


@router.get("/kbs", dependencies=[Depends(require_manager)])
def list_kbs(db: Session = Depends(get_db), page: int = Query(1, ge=1), pageSize: int = Query(50, ge=1, le=200)):
    total = db.scalar(select(func.count()).select_from(KnowledgeBase))
    return {"items": [kb_dto(k, db) for k in db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.name).offset((page-1)*pageSize).limit(pageSize))], "total": total}


@router.post("/kbs", dependencies=[Depends(require_manager)], status_code=201)
def create_kb(payload: KBCreate, db: Session = Depends(get_db)):
    require_model(db, payload.model, "Embedding")
    v = payload.model_dump(); v["model_id"] = v.pop("model")
    item = KnowledgeBase(**v); db.add(item); db.commit()
    return kb_dto(item, db, True)


@router.patch("/kbs/{id}", dependencies=[Depends(require_manager)])
def update_kb(id: str, payload: KBUpdate, db: Session = Depends(get_db)):
    item = get_or_404(db, KnowledgeBase, id)
    v = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "model" in v:
        require_model(db, v["model"], "Embedding")
        if v["model"] != item.model_id and db.scalar(select(func.count()).select_from(DocumentRecord).where(DocumentRecord.kb_id == id)):
            raise HTTPException(409, "已有文档的知识库不允许直接更换向量模型，请创建新知识库并重新导入以保持索引一致")
        v["model_id"] = v.pop("model")
    for key, value in v.items(): setattr(item, key, value)
    db.commit(); return kb_dto(item, db, True)


@router.delete("/kbs/{id}", dependencies=[Depends(require_manager)])
def delete_kb(id: str, db: Session = Depends(get_db)):
    item = get_or_404(db, KnowledgeBase, id)
    for assistant in db.scalars(select(Assistant)):
        if id in assistant.kb_ids:
            assistant.kb_ids = [x for x in assistant.kb_ids if x != id]
            if not assistant.kb_ids: assistant.published = False
    # SQL foreign-key cascades remove documents, chunks and jobs. Persistent
    # vector payloads are filtered against SQL before retrieval; stale collections
    # can be removed offline without serving deleted material.
    db.delete(item)
    index_setting = db.get(Setting, "kb_index:" + id)
    if index_setting: db.delete(index_setting)
    db.commit(); return {"ok": True}


@router.post("/models", dependencies=[Depends(require_admin)], status_code=201)
def create_model(payload: ModelCreate, db: Session = Depends(get_db)):
    if payload.type == "Embedding" and not payload.dim:
        raise HTTPException(422, "Embedding 模型需要配置向量维度")
    v = payload.model_dump(); key = v.pop("apiKey"); v["model_id"] = v.pop("modelId"); v["dimension"] = v.pop("dim")
    item = ModelConfig(**v, encrypted_key=encrypt(key)); db.add(item); db.commit()
    return model_dto(item)


@router.patch("/models/{id}", dependencies=[Depends(require_admin)])
def update_model(id: str, payload: ModelUpdate, db: Session = Depends(get_db)):
    item = get_or_404(db, ModelConfig, id)
    v = payload.model_dump(exclude_unset=True, exclude_none=True)
    identity = {"provider": "provider", "type": "type", "modelId": "model_id", "base": "base", "dim": "dimension"}
    changing_identity = any(key in v and v[key] != getattr(item, attr) for key, attr in identity.items())
    if changing_identity and db.scalar(select(func.count()).select_from(DocumentRecord).join(KnowledgeBase).where(KnowledgeBase.model_id == id)):
        raise HTTPException(409, "此向量模型已有知识库文档使用；请新建模型配置并重新导入，不能覆盖现有索引身份")
    if "type" in v and v["type"] != item.type:
        bound = db.scalar(select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.model_id == id)) or db.scalar(select(func.count()).select_from(Assistant).where(Assistant.model_id == id))
        if bound: raise HTTPException(409, "模型已被知识库或助手引用，不能修改用途")
    key = v.pop("apiKey", "")
    if key: item.encrypted_key = encrypt(key)
    if "modelId" in v: v["model_id"] = v.pop("modelId")
    if "dim" in v: v["dimension"] = v.pop("dim")
    final_type = v.get("type", item.type)
    if final_type == "Embedding" and not v.get("dimension", item.dimension):
        raise HTTPException(422, "Embedding 模型需要配置向量维度")
    for field, value in v.items(): setattr(item, field, value)
    if changing_identity or key:
        item.options = {**(item.options or {}), "tested": False}
    db.commit(); return model_dto(item)


@router.delete("/models/{id}", dependencies=[Depends(require_admin)])
def delete_model(id: str, db: Session = Depends(get_db)):
    item = get_or_404(db, ModelConfig, id)
    if db.scalar(select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.model_id == id)) or db.scalar(select(func.count()).select_from(Assistant).where(Assistant.model_id == id)):
        raise HTTPException(409, "此模型已被知识库或助手引用，不能删除")
    db.delete(item); db.commit(); return {"ok": True}


@router.post("/models/{id}/test", dependencies=[Depends(require_admin)])
async def test_model(id: str, db: Session = Depends(get_db)):
    item = get_or_404(db, ModelConfig, id)
    started = time.monotonic(); llm = None
    try:
        details = {}
        if item.type == "Embedding":
            vectors = await asyncio.to_thread(embedding_model(item, db).embed_documents, ["企业知识测试", "knowledge test"])
            details = {"dimension": len(vectors[0]), "count": len(vectors)}
        elif item.type == "Rerank":
            result = await rerank_documents(item, "知识库", ["企业知识库支持检索", "今天天气晴朗"], db)
            details = {"count": len(result)}
        else:
            llm = await chat_model(item, db)
            parts, usage = [], None
            async for part in llm.astream("只回复 OK，用于连接测试。"):
                if isinstance(part.content, str): parts.append(part.content)
                if part.usage_metadata: usage = part.usage_metadata
            if not "".join(parts).strip(): raise ProviderError("LLM 未返回文本内容")
            db.add(LogEntry(kind="模型调用", question="LLM 连接测试", model=item.name, tokens=(usage or {}).get("total_tokens"), duration=time.monotonic()-started, status="成功", details={"usage": usage, "purpose": "connection_test"}))
            details = {"stream": True, "usage": usage}
        item.options = {**(item.options or {}), "tested": True}
        db.commit()
        return {"ok": True, "message": "真实连接测试通过", "details": details}
    except Exception as exc:
        item.options = {**(item.options or {}), "tested": False}
        safe = str(exc) if isinstance(exc, ProviderError) else "模型连接失败，请检查地址、地区、模型、密钥和网络"
        db.add(LogEntry(kind="模型调用", question="连接测试", model=item.name, tokens=None, duration=time.monotonic()-started, status="失败", details={"error": safe}))
        db.commit()
        raise HTTPException(502, safe) from exc
    finally:
        if llm:
            await llm.http_async_client.aclose(); llm.http_client.close()


def validate_assistant(db, v, existing=None):
    require_model(db, v.get("model", existing.model_id if existing else ""), "LLM")
    ids = v.get("kbIds", existing.kb_ids if existing else [])
    if not ids or any(not db.get(KnowledgeBase, id) for id in ids):
        raise HTTPException(422, "请关联至少一个有效知识库")
    if v.get("rerank", existing.rerank if existing else False):
        options = v.get("options", existing.options if existing else {}) or {}
        if options.get("rerankModelId"):
            require_model(db, options["rerankModelId"], "Rerank")
        if not db.scalar(select(ModelConfig).where(ModelConfig.type == "Rerank", ModelConfig.enabled == True)):
            raise HTTPException(422, "启用重排前请配置一个 Rerank 模型")


@router.post("/assistants", dependencies=[Depends(require_manager)], status_code=201)
def create_assistant(payload: AssistantCreate, db: Session = Depends(get_db)):
    v = payload.model_dump(); validate_assistant(db, v)
    v["kb_ids"] = list(dict.fromkeys(v.pop("kbIds"))); v["model_id"] = v.pop("model")
    item = Assistant(**v); db.add(item); db.commit(); return assistant_dto(item)


@router.patch("/assistants/{id}", dependencies=[Depends(require_manager)])
def update_assistant(id: str, payload: AssistantUpdate, db: Session = Depends(get_db)):
    item = get_or_404(db, Assistant, id)
    v = payload.model_dump(exclude_unset=True, exclude_none=True); validate_assistant(db, v, item)
    if "kbIds" in v: v["kb_ids"] = list(dict.fromkeys(v.pop("kbIds")))
    if "model" in v: v["model_id"] = v.pop("model")
    for key, value in v.items(): setattr(item, key, value)
    db.commit(); return assistant_dto(item)


@router.put("/ocr", dependencies=[Depends(require_admin)])
def update_ocr(payload: OCRUpdate, db: Session = Depends(get_db)):
    item = db.get(Setting, "ocr")
    old = item.value if item else {}
    endpoint = payload.endpoint or "docmind-api." + payload.region + ".aliyuncs.com"
    if endpoint != "docmind-api." + payload.region + ".aliyuncs.com":
        raise HTTPException(422, "Document Mind endpoint 必须与所选地域一致")
    v = {"region": payload.region, "endpoint": endpoint,
         "encryptedAccessKeyId": encrypt(payload.accessKeyId) if payload.accessKeyId else old.get("encryptedAccessKeyId", ""),
         "encryptedAccessKeySecret": encrypt(payload.accessKeySecret) if payload.accessKeySecret else old.get("encryptedAccessKeySecret", "")}
    if not v["encryptedAccessKeyId"] or not v["encryptedAccessKeySecret"]:
        raise HTTPException(422, "首次配置需要 AccessKey ID 与 Secret")
    if not item: item = Setting(key="ocr", value=v); db.add(item)
    else: item.value = v
    db.commit(); return ocr_dto(db)


@router.get("/logs", dependencies=[Depends(require_admin)])
def logs(kind: str = "", query: str = "", page: int = Query(1, ge=1), pageSize: int = Query(50, ge=1, le=500), export: bool = False, db: Session = Depends(get_db)):
    stmt = select(LogEntry)
    if kind and kind != "全部": stmt = stmt.where(LogEntry.kind == kind)
    if query: stmt = stmt.where(LogEntry.question.contains(query[:200], autoescape=True))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    values = [log_dto(l) for l in db.scalars(stmt.order_by(LogEntry.time.desc()).offset((page-1)*pageSize).limit(10000 if export else pageSize))]
    if export: return JSONResponse(values, headers={"Content-Disposition": 'attachment; filename="zhixu-logs.json"'})
    return {"items": values, "total": total, "page": page, "pageSize": pageSize}

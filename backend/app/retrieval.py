"""Persistent Qdrant dense + sparse retrieval with final SQL validity checks."""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import threading
from collections import Counter

import jieba
from qdrant_client import QdrantClient, models as qm
from sqlalchemy import select

from .config import settings
from .models import ChunkRecord, DocumentRecord, KnowledgeBase, ModelConfig, Setting

_client = None
_client_lock = threading.RLock()


def qdrant() -> QdrantClient:
    global _client
    with _client_lock:
        if _client is None:
            if settings.qdrant_url:
                _client = QdrantClient(url=settings.qdrant_url, timeout=60)
            else:
                if settings.task_mode == 'celery':
                    raise ValueError('Celery 模式必须配置独立 Qdrant 服务，不能共享本地数据库文件')
                _client = QdrantClient(path=str(settings.data_dir / 'qdrant'), force_disable_check_same_thread=True)
        return _client


def embedding_fingerprint(model: ModelConfig) -> str:
    raw = f'{model.provider}\0{model.base}\0{model.model_id}\0{model.dimension}'
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def collection_name(kb: KnowledgeBase, model: ModelConfig) -> str:
    return 'kb_' + hashlib.sha256(kb.id.encode()).hexdigest()[:16] + '_' + embedding_fingerprint(model)


def tokens(text: str) -> list[str]:
    return [word.lower() for word in jieba.cut(text) if re.search(r'[\w\u4e00-\u9fff]', word) and word.strip()]


def sparse_vector(text: str, *, query: bool = False):
    counts = Counter(tokens(text))
    weights = {}
    length = max(1, sum(counts.values()))
    for token, tf in counts.items():
        index = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), 'big')
        # BM25 term frequency; Qdrant applies corpus IDF at query time.
        weight = 1.0 if query else tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / 256))
        weights[index] = weights.get(index, 0) + weight
    indexes = sorted(weights)
    return qm.SparseVector(indices=indexes, values=[weights[i] for i in indexes])


def ensure_collection(kb: KnowledgeBase, model: ModelConfig, dimension: int) -> str:
    client, name = qdrant(), collection_name(kb, model)
    if not client.collection_exists(name):
        try:
            client.create_collection(collection_name=name, vectors_config={'dense': qm.VectorParams(size=dimension, distance=qm.Distance.COSINE)}, sparse_vectors_config={'keywords': qm.SparseVectorParams(modifier=qm.Modifier.IDF)})
        except Exception:
            if not client.collection_exists(name):
                raise
    return name


def index_chunks(db, kb: KnowledgeBase, chunks: list[ChunkRecord], model: ModelConfig, progress=None) -> None:
    from .providers import embedding_model
    if not model.enabled or model.type != 'Embedding':
        raise ValueError('知识库绑定的 Embedding 模型不可用')
    embeddings = embedding_model(model, db)
    dimension = model.dimension
    name = None
    enabled = [chunk for chunk in chunks if chunk.enabled]
    if not enabled:
        raise ValueError('没有启用的知识片段')
    for start in range(0, len(enabled), 16):
        batch = enabled[start:start + 16]
        try:
            vectors = embeddings.embed_documents([c.text for c in batch])
        except Exception:
            # Preserve provider failure/usage records; staged chunks remain unpublished.
            if db.is_active:
                db.commit()
            raise
        if len(vectors) != len(batch):
            raise ValueError('Embedding 返回向量条数不匹配')
        measured = len(vectors[0]) if vectors else 0
        if not measured or any(len(v) != measured or not all(math.isfinite(x) for x in v) for v in vectors):
            raise ValueError('Embedding 返回无效向量')
        if dimension and dimension != measured:
            raise ValueError(f'Embedding 实际维度 {measured} 与配置 {dimension} 不一致')
        dimension = measured
        name = name or ensure_collection(kb, model, dimension)
        points = [qm.PointStruct(id=c.id, vector={'dense': vector, 'keywords': sparse_vector(c.text)}, payload={'kbId': kb.id, 'documentId': c.document_id, 'version': c.version, 'fingerprint': embedding_fingerprint(model), 'chunkId': c.id}) for c, vector in zip(batch, vectors)]
        qdrant().upsert(collection_name=name, points=points, wait=True)
        if progress:
            progress(start + len(batch), len(enabled))
    fingerprint = embedding_fingerprint(model)
    state = db.get(Setting, 'kb_index:' + kb.id)
    if state:
        state.value = {'fingerprint': fingerprint, 'collection': name, 'dimension': dimension}
    else:
        db.add(Setting(key='kb_index:' + kb.id, value={'fingerprint': fingerprint, 'collection': name, 'dimension': dimension}))
    db.flush()


def _valid_results(db, hits):
    ids = [hit.id if hasattr(hit, 'id') else hit for hit in hits]
    if not ids:
        return {}
    records = db.execute(select(ChunkRecord, DocumentRecord, KnowledgeBase).join(DocumentRecord, DocumentRecord.id == ChunkRecord.document_id).join(KnowledgeBase, KnowledgeBase.id == DocumentRecord.kb_id).where(ChunkRecord.id.in_([str(i) for i in ids]), ChunkRecord.enabled.is_(True), DocumentRecord.status == 'ready', KnowledgeBase.enabled.is_(True), ChunkRecord.version == DocumentRecord.active_version)).all()
    return {chunk.id: {'doc': {'id': doc.id, 'name': doc.name}, 'chunk': {'id': chunk.id, 'text': chunk.text, 'location': chunk.location, 'enabled': chunk.enabled}, 'documentId': doc.id, 'version': chunk.version, 'kbId': kb.id} for chunk, doc, kb in records}


def retrieve(db, question: str, kb_ids: list[str], mode: str = 'hybrid', rerank: bool = True, top_k: int = 5, *, rerank_model_id: str | None = None) -> list[dict]:
    from .providers import embedding_model, rerank_documents
    if mode not in {'hybrid', 'vector', 'keyword'}:
        raise ValueError('未知检索策略')
    if not question.strip() or not kb_ids:
        return []
    top_k = max(1, min(20, top_k))
    kbs = db.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids), KnowledgeBase.enabled.is_(True))).all()
    ranks, scores, per_kb_modes = [], {}, []
    for kb in kbs:
        model = db.get(ModelConfig, kb.model_id)
        if not model or not model.enabled:
            raise ValueError(f'知识库“{kb.name}”的 Embedding 配置不可用')
        state = db.get(Setting, 'kb_index:' + kb.id)
        if not state:
            continue
        if state.value.get('fingerprint') != embedding_fingerprint(model):
            raise ValueError(f'知识库“{kb.name}”的 Embedding 配置已变化，请重建索引')
        name = state.value['collection']
        filtered = qm.Filter(must=[qm.FieldCondition(key='kbId', match=qm.MatchValue(value=kb.id))])
        paths = []
        # Oversample because stale/deactivated generations are filtered again in SQL.
        if mode in {'hybrid', 'vector'}:
            vector = embedding_model(model, db).embed_query(question)
            hits = qdrant().query_points(collection_name=name, query=vector, using='dense', query_filter=filtered, limit=max(50, top_k * 10)).points
            paths.append(hits)
        if mode in {'hybrid', 'keyword'}:
            vector = sparse_vector(question, query=True)
            if vector.indices:
                hits = qdrant().query_points(collection_name=name, query=vector, using='keywords', query_filter=filtered, limit=max(50, top_k * 10)).points
                paths.append(hits)
        for hits in paths:
            valid = _valid_results(db, hits)
            rank = [str(hit.id) for hit in hits if str(hit.id) in valid]
            if rank:
                ranks.append(rank)
                for idx, cid in enumerate(rank):
                    scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + idx + 1)
    if not scores:
        return []
    ordered = sorted(scores, key=scores.get, reverse=True)[:max(20, top_k * 3)]
    valid = _valid_results(db, ordered)
    results = [{**valid[cid], 'score': round(scores[cid], 7)} for cid in ordered if cid in valid]
    if rerank and results:
        if rerank_model_id:
            model = db.get(ModelConfig, rerank_model_id)
        else:
            model = db.scalar(select(ModelConfig).where(ModelConfig.type == 'Rerank', ModelConfig.enabled.is_(True)).order_by(ModelConfig.id))
        if not model or model.type != 'Rerank' or not model.enabled:
            raise ValueError('启用了 Rerank，但未配置可用的重排模型')
        ranked = asyncio.run(rerank_documents(model, question, [r['chunk']['text'] for r in results], db))
        return [{**results[index], 'score': score} for index, score in ranked[:top_k]]
    return results[:top_k]


def cleanup_document(document_id: str, kb_id: str, *, keep_version: str | None = None):
    """Best-effort index cleanup; SQL invalidation is always authoritative."""
    client = qdrant()
    names = [c.name for c in client.get_collections().collections if c.name.startswith('kb_' + hashlib.sha256(kb_id.encode()).hexdigest()[:16])]
    for name in names:
        conditions = [qm.FieldCondition(key='documentId', match=qm.MatchValue(value=document_id))]
        filtered = qm.Filter(must=conditions, must_not=[qm.FieldCondition(key='version', match=qm.MatchValue(value=keep_version))] if keep_version else None)
        client.delete(collection_name=name, points_selector=qm.FilterSelector(filter=filtered), wait=True)

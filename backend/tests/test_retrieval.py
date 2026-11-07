from uuid import uuid4
import pytest
from langchain_core.embeddings import Embeddings
from sqlalchemy import select
from app.models import ChunkRecord, DocumentRecord, ImportJob, KnowledgeBase, ModelConfig
from app.ingestion import make_job, process_job
from app.retrieval import index_chunks, retrieve


class TestEmbeddings(Embeddings):
    """Deterministic vectors for isolated tests; never available in product config."""
    def embed_documents(self, texts):
        return [[float('年假' in text) + .1, float('福利' in text) + .1, .1] for text in texts]
    def embed_query(self, text):
        return self.embed_documents([text])[0]


def seed_index(db, monkeypatch):
    import app.providers
    monkeypatch.setattr(app.providers, 'embedding_model', lambda *_: TestEmbeddings())
    model = ModelConfig(id=str(uuid4()), name='embedding', provider='AIHubMix', type='Embedding', model_id='test-model', base='https://example.com/v1', dimension=3, enabled=True)
    db.add(model)
    db.flush()
    kb = KnowledgeBase(id=str(uuid4()), name='hr', model_id=model.id, enabled=True)
    db.add(kb)
    db.flush()
    doc = DocumentRecord(id=str(uuid4()), kb_id=kb.id, name='policy.md', kind='md', status='ready', active_version='v1')
    db.add(doc)
    db.flush()
    chunks = [ChunkRecord(id=str(uuid4()), document_id=doc.id, version='v1', text='年假申请应提前三天，并交负责人审批。', location='第 2 行', enabled=True), ChunkRecord(id=str(uuid4()), document_id=doc.id, version='v1', text='年度体检是员工福利，每年一次。', location='第 4 行', enabled=True)]
    db.add_all(chunks)
    db.commit()
    index_chunks(db, kb, chunks, model)
    db.commit()
    return kb, doc, chunks


def test_persistent_hybrid_and_sql_invalidation(db, monkeypatch):
    kb, doc, chunks = seed_index(db, monkeypatch)
    found = retrieve(db, '年假申请', [kb.id], rerank=False)
    assert found and found[0]['chunk']['id'] == chunks[0].id
    chunks[0].enabled = False
    db.commit()
    assert all(item['chunk']['id'] != chunks[0].id for item in retrieve(db, '年假', [kb.id], rerank=False))
    doc.status = 'deleted'
    db.commit()
    assert retrieve(db, '福利', [kb.id], rerank=False) == []


def test_old_versions_and_other_libraries_never_leak(db, monkeypatch):
    kb, doc, chunks = seed_index(db, monkeypatch)
    assert retrieve(db, '年假', [str(uuid4())], rerank=False) == []
    doc.active_version = 'not-yet-indexed'
    db.commit()
    assert retrieve(db, '年假', [kb.id], rerank=False) == []


def test_rerank_missing_configuration_explicit(db, monkeypatch):
    kb, doc, chunks = seed_index(db, monkeypatch)
    with pytest.raises(ValueError, match='重排模型'):
        retrieve(db, '年假', [kb.id], rerank=True)


def test_failed_rebuild_preserves_published_version(db, monkeypatch, tmp_path):
    kb, doc, chunks = seed_index(db, monkeypatch)
    import app.retrieval
    path = tmp_path / 'policy.md'
    path.write_text('# 年假\n更新后的请假流程。', encoding='utf-8')
    doc.path = str(path)
    job = make_job(db, doc, {'cloudParser': False, 'autoBuild': True})
    db.commit()
    def broken(*args, **kwargs):
        raise ValueError('服务配置暂时不可用')
    monkeypatch.setattr(app.retrieval, 'index_chunks', broken)
    process_job(job.id)
    db.expire_all()
    assert db.get(ImportJob, job.id).status == 'failed'
    current = db.get(DocumentRecord, doc.id)
    assert current.status == 'ready'
    assert current.active_version == 'v1'
    assert retrieve(db, '年假申请', [kb.id], rerank=False)[0]['chunk']['id'] == chunks[0].id


def test_duplicate_dispatch_claim_is_idempotent(db, monkeypatch, tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import app.ingestion
    from app.models import LogEntry
    kb, doc, chunks = seed_index(db, monkeypatch)
    path = tmp_path / 'policy.md'
    path.write_text('# Policy\n年假申请的新版本。', encoding='utf-8')
    doc.path = str(path)
    job = make_job(db, doc, {'cloudParser': False, 'autoBuild': True})
    db.commit()
    calls = []
    lock = threading.Lock()
    real_parse = app.ingestion.parse_file
    def counted(*args, **kwargs):
        with lock:
            calls.append(1)
        return real_parse(*args, **kwargs)
    monkeypatch.setattr(app.ingestion, 'parse_file', counted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(process_job, job.id) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    db.expire_all()
    assert db.get(ImportJob, job.id).status == 'completed'
    assert len(calls) == 1
    assert len(db.scalars(select(LogEntry).where(LogEntry.kind == '导入')).all()) == 1


def test_cleanup_failures_retried_from_database(db, monkeypatch):
    import app.retrieval
    from app.tasks import retry_cleanup
    kb, doc, chunks = seed_index(db, monkeypatch)
    job = ImportJob(document_id=doc.id, status='completed', stage='completed', progress=100, options={'cleanupPending': True})
    db.add(job)
    db.commit()
    calls = []
    monkeypatch.setattr(app.retrieval, 'cleanup_document', lambda *args, **kwargs: calls.append((args, kwargs)))
    assert retry_cleanup() == 1
    db.expire_all()
    assert db.get(ImportJob, job.id).options['cleanupPending'] is False
    assert calls[0][1]['keep_version'] == doc.active_version

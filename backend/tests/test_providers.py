import json
import httpx
import pytest
from sqlalchemy import select
from app.models import ModelConfig, LogEntry
from app.providers import embedding_model, rerank_documents, ProviderError
from app.security import encrypt


def model(db, type, provider="OpenRouter", dim=None):
    m = ModelConfig(name="test", provider=provider, type=type, model_id="test-model", base="http://127.0.0.1/api/v1", dimension=dim, encrypted_key=encrypt("test-key"), enabled=True)
    db.add(m); db.commit(); return m


def test_embedding_dimension_count_and_usage_validation(db, monkeypatch):
    m = model(db, "Embedding", dim=3); original = httpx.Client; seen = []
    def handler(request):
        body = json.loads(request.content); seen.append(body)
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [1, 0, 0]} for i in range(len(body["input"]))], "usage": {"total_tokens": 9}})
    monkeypatch.setattr("app.providers.httpx.Client", lambda **kw: original(transport=httpx.MockTransport(handler)))
    assert embedding_model(m, db).embed_documents(["企业制度", "报销制度"]) == [[1, 0, 0], [1, 0, 0]]
    assert seen[0]["input"] == ["企业制度", "报销制度"] and db.scalar(select(LogEntry)).tokens == 9
    m.dimension = 4
    with pytest.raises(ProviderError, match="维度"): embedding_model(m).embed_query("test")


def test_embedding_rejects_duplicate_indices(db, monkeypatch):
    m = model(db, "Embedding", dim=2); original = httpx.Client
    monkeypatch.setattr("app.providers.httpx.Client", lambda **kw: original(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [1, 0]}]}))))
    with pytest.raises(ProviderError, match="索引"): embedding_model(m).embed_documents(["one", "two"])


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,native", [("OpenRouter", False), ("AIHubMix", False), ("通义百炼", True)])
async def test_rerank_protocol_and_missing_usage(db, monkeypatch, provider, native):
    m = model(db, "Rerank", provider=provider); original = httpx.AsyncClient; seen = []
    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        result = [{"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .1}]
        return httpx.Response(200, json={"output": {"results": result}} if native else {"results": result})
    monkeypatch.setattr("app.providers.httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler)))
    assert await rerank_documents(m, "question", ["A", "B"], db) == [(1, .9), (0, .1)]
    assert ("input" in seen[0][1]) == native
    assert seen[0][0].endswith("/services/rerank/text-rerank/text-rerank" if native else "/rerank")
    assert db.scalar(select(LogEntry)).tokens is None


@pytest.mark.asyncio
async def test_rerank_rejects_out_of_range_index(db, monkeypatch):
    m = model(db, "Rerank"); original = httpx.AsyncClient
    monkeypatch.setattr("app.providers.httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [{"index": 8, "relevance_score": .9}]}))))
    with pytest.raises(ProviderError, match="索引"): await rerank_documents(m, "query", ["text"])

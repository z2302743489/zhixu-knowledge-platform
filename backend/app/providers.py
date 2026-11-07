"""LangChain LLM/Embedding and provider-specific Rerank adapters.

References: OpenRouter /api/v1/rerank; AIHubMix /v1/rerank;
DashScope native /api/v1/services/rerank/text-rerank/text-rerank.
There are no product mocks or local model weights.
"""
import math
import time
from typing import Iterable
import httpx
from langchain_core.embeddings import Embeddings
from langchain_openai import ChatOpenAI
from sqlalchemy.orm import Session
from .config import settings
from .observability import trace_context
from .models import LogEntry, ModelConfig
from .security import decrypt, validate_provider_url


class ProviderError(ValueError):
    pass


def _ready(config: ModelConfig):
    if not config or not config.enabled:
        raise ProviderError("模型未配置或已停用")
    if not config.encrypted_key:
        raise ProviderError(f"请先配置模型 {config.name} 的 API Key")
    if not config.model_id.strip():
        raise ProviderError("请填写实际模型 ID")
    return validate_provider_url(config.base, resolve=True), decrypt(config.encrypted_key)


def _error(response: httpx.Response):
    if response.is_error:
        # Never echo remote response bodies; they may contain credentials/content.
        raise ProviderError(f"服务商返回 HTTP {response.status_code}，请检查模型 ID、地区、密钥与额度")


def _usage(data: dict) -> int | None:
    usage = data.get("usage") or {}
    result = usage.get("total_tokens")
    if result is None and isinstance(usage.get("input_tokens"), int):
        result = usage["input_tokens"] + (usage.get("output_tokens") or 0)
    return result if isinstance(result, int) and result >= 0 else None


def _log(db, config, purpose, started, data=None, error=None):
    if db is None:
        return
    trace = trace_context.get()
    db.add(LogEntry(kind="模型调用", question=trace.get("question", purpose), model=config.name, owner=trace.get("owner", ""),
                    tokens=_usage(data or {}), duration=time.monotonic() - started,
                    status="失败" if error else "成功",
                    details={"purpose": purpose, "trace_id": trace.get("trace_id"), "provider": config.provider, "modelId": config.model_id,
                             "requestId": (data or {}).get("request_id") or (data or {}).get("id"),
                             "usage": (data or {}).get("usage"), "error": error}))
    db.flush()


async def chat_model(config: ModelConfig, db: Session | None = None) -> ChatOpenAI:
    base, key = _ready(config)
    if config.type != "LLM":
        raise ProviderError("所选模型不是 LLM")
    opts = config.options or {}
    return ChatOpenAI(model=config.model_id, api_key=key, base_url=base,
                      timeout=min(float(opts.get("timeout", settings.provider_timeout)), 300),
                      max_retries=1, streaming=True, stream_usage=True,
                      http_client=httpx.Client(follow_redirects=False, trust_env=False),
                      http_async_client=httpx.AsyncClient(follow_redirects=False, trust_env=False),
                      temperature=float(opts.get("temperature", 0.2)),
                      max_tokens=min(int(opts.get("maxTokens", 2048)), 8192))


class ProviderEmbeddings(Embeddings):
    def __init__(self, config: ModelConfig, db=None):
        self.config, self.db = config, db
        self.base, self.key = _ready(config)
        if config.type != "Embedding":
            raise ProviderError("所选模型不是 Embedding")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        batch = min(max(int((self.config.options or {}).get("batchSize", 16)), 1), 64)
        for offset in range(0, len(texts), batch):
            part = texts[offset:offset + batch]
            started = time.monotonic()
            try:
                body = {"model": self.config.model_id, "input": part, "encoding_format": "float"}
                if (self.config.options or {}).get("sendDimensions") and self.config.dimension:
                    body["dimensions"] = self.config.dimension
                with httpx.Client(timeout=settings.provider_timeout, follow_redirects=False, trust_env=False) as client:
                    response = client.post(self.base + "/embeddings", json=body, headers={"Authorization": "Bearer " + self.key})
                _error(response)
                data = response.json()
                if not isinstance(data, dict):
                    raise ProviderError("Embedding 返回格式无效")
                rows = data.get("data", [])
                if len(rows) != len(part):
                    raise ProviderError("Embedding 返回向量数量与输入不一致")
                indices = [row.get("index") for row in rows]
                if sorted(indices) != list(range(len(part))):
                    raise ProviderError("Embedding 返回索引不完整")
                values = [row["embedding"] for row in sorted(rows, key=lambda r: r["index"])]
                for vector in values:
                    if not vector or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector):
                        raise ProviderError("Embedding 返回非法向量")
                    if self.config.dimension and len(vector) != self.config.dimension:
                        raise ProviderError(f"Embedding 实际维度 {len(vector)} 与配置 {self.config.dimension} 不一致")
                if len({len(v) for v in values}) != 1:
                    raise ProviderError("Embedding 返回维度不一致")
                vectors.extend(values)
                _log(self.db, self.config, "embedding", started, data)
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                err = str(exc) if isinstance(exc, ProviderError) else "Embedding 调用失败，请检查服务配置与网络"
                _log(self.db, self.config, "embedding", started, error=err)
                raise ProviderError(err) from exc
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def embedding_model(config: ModelConfig, db: Session | None = None) -> Embeddings:
    return ProviderEmbeddings(config, db)


async def rerank_documents(config: ModelConfig, query: str, texts: list[str], db: Session | None = None) -> list[tuple[int, float]]:
    if not texts:
        return []
    base, key = _ready(config)
    if config.type != "Rerank":
        raise ProviderError("所选模型不是 Rerank")
    opts = config.options or {}
    started = time.monotonic()
    native = config.provider == "通义百炼" and opts.get("protocol", "dashscope") == "dashscope"
    if native:
        suffix = "/services/rerank/text-rerank/text-rerank"
        endpoint = base + suffix if base.endswith("/api/v1") else base
        if endpoint == base and not base.endswith(suffix):
            raise ProviderError("百炼原生排序地址需填写 /api/v1 根地址或完整 text-rerank 地址")
        body = {"model": config.model_id, "input": {"query": query, "documents": texts},
                "parameters": {"top_n": len(texts), "return_documents": False}}
    else:
        endpoint = base + ("/reranks" if opts.get("protocol") == "dashscope-compatible" else "/rerank")
        body = {"model": config.model_id, "query": query, "documents": texts, "top_n": len(texts)}
    try:
        async with httpx.AsyncClient(timeout=settings.provider_timeout, follow_redirects=False, trust_env=False) as client:
            response = await client.post(endpoint, json=body, headers={"Authorization": "Bearer " + key})
        _error(response)
        data = response.json()
        if not isinstance(data, dict):
            raise ProviderError("Rerank 返回格式无效")
        results = data.get("output", {}).get("results", []) if native else data.get("results", [])
        ranked, seen = [], set()
        for item in results:
            index, score = item["index"], item["relevance_score"]
            if not isinstance(index, int) or index not in range(len(texts)) or index in seen or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise ProviderError("Rerank 返回无效或重复候选索引")
            seen.add(index)
            ranked.append((index, float(score)))
        if not ranked:
            raise ProviderError("Rerank 未返回候选结果")
        _log(db, config, "rerank", started, data)
        return sorted(ranked, key=lambda r: r[1], reverse=True)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        err = str(exc) if isinstance(exc, ProviderError) else "Rerank 调用失败，请检查服务配置与网络"
        _log(db, config, "rerank", started, error=err)
        raise ProviderError(err) from exc

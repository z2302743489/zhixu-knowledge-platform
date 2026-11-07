"""Per-request context propagated by asyncio.to_thread to provider adapters."""
from contextvars import ContextVar

trace_context: ContextVar[dict] = ContextVar("zhixu_trace_context", default={})

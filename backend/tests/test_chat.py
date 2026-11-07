import json
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from sqlalchemy import select
from app.models import Assistant, ChatMessage, ChatSession, LogEntry, ModelConfig


def setup_chat(db):
    db.add(ModelConfig(id="llm", name="Test model", provider="OpenRouter", type="LLM", model_id="fixture", base="https://openrouter.ai/api/v1"))
    db.flush()
    db.add(Assistant(id="assistant", name="Test assistant", kb_ids=[], model_id="llm", rerank=False, published=True))
    db.commit()


def events(response):
    result = []
    for block in response.text.strip().split("\n\n"):
        lines = block.splitlines()
        kind = next(line[7:] for line in lines if line.startswith("event: "))
        payload = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        result.append((kind, payload))
    return result


def install_model(monkeypatch):
    import app.chat as module
    async def fake_model(*_):
        return FakeListChatModel(responses=["来自资料的回答 [1]。虚构引用 [999]。"])
    monkeypatch.setattr(module, "chat_model", fake_model)
    monkeypatch.setattr(module, "retrieve_in_thread", lambda *_: [
        {"doc": {"id": "doc-1", "name": "真实导入测试.md"}, "version": "v1", "chunk":
         {"id": "chunk-1", "text": "年假通过 OA 申请。", "location": "第 1 行", "enabled": True}, "score": 1}])


def test_grounded_stream_and_owned_history(client, db, monkeypatch):
    setup_chat(db)
    install_model(monkeypatch)
    response = client.post("/api/chat", json={"assistantId": "assistant", "question": "如何申请年假"})
    assert response.status_code == 200
    parsed = events(response)
    meta = next(data for kind, data in parsed if kind == "meta")
    done = next(data for kind, data in parsed if kind == "done")
    assert done["status"] == "completed"
    assert "[1]" in done["text"] and "[999]" not in done["text"]
    assert done["refs"][0]["documentId"] == "doc-1"
    db.expire_all()
    answer = db.get(ChatMessage, done["messageId"])
    assert answer.text == done["text"]
    log = db.get(LogEntry, meta["runId"])
    assert log.tokens is None  # Missing provider usage never becomes fabricated zero.
    assert log.duration > 0 and log.details["stages"]["generation"] >= 0
    assert client.patch("/api/messages/" + done["messageId"], json={"feedback": "up"}).status_code == 200
    # A different anonymous browser cannot read, continue, cancel, or rate this session.
    original = client.cookies.get("zhixu_session")
    client.cookies.clear()
    assert client.post("/api/chat", json={"assistantId": "assistant", "sessionId": meta["sessionId"], "question": "继续"}).status_code == 404
    assert client.post("/api/chat/" + meta["runId"] + "/cancel").status_code == 404
    assert client.patch("/api/messages/" + done["messageId"], json={"feedback": "up"}).status_code == 404


def test_no_evidence_does_not_generate(client, db, monkeypatch):
    setup_chat(db)
    install_model(monkeypatch)
    import app.chat as module
    monkeypatch.setattr(module, "retrieve_in_thread", lambda *_: [])
    response = client.post("/api/chat", json={"assistantId": "assistant", "question": "资料以外的问题"})
    done = next(data for kind, data in events(response) if kind == "done")
    assert done["refs"] == [] and "没有找到" in done["text"]
    assert db.scalar(select(LogEntry).where(LogEntry.kind == "模型调用")) is None


def test_unpublished_assistant_and_empty_question(client, db):
    setup_chat(db)
    assistant = db.get(Assistant, "assistant")
    assistant.published = False
    db.commit()
    assert client.post("/api/chat", json={"assistantId": "assistant", "question": "question"}).status_code == 404
    assert client.post("/api/chat", json={"assistantId": "assistant", "question": " "}).status_code == 422


def test_cancel_before_retrieval_yields_done_and_persists(client, db, monkeypatch):
    setup_chat(db)
    install_model(monkeypatch)
    import app.chat as module
    monkeypatch.setattr(module, "cancel_requested", lambda _: True)
    response = client.post("/api/chat", json={"assistantId": "assistant", "question": "取消这个问题"})
    parsed = events(response)
    assert [kind for kind, _ in parsed] == ["meta", "done"]
    assert parsed[-1][1]["status"] == "cancelled"
    db.expire_all()
    log = db.get(LogEntry, parsed[0][1]["runId"])
    assert log.status == "cancelled" and log.tokens is None
    assert log.details["usageComplete"] is False
    assert db.get(ChatMessage, parsed[-1][1]["messageId"]).status == "cancelled"


def test_wrong_owner_rejected_before_allocating_model(client, db, monkeypatch):
    setup_chat(db)
    db.add(ChatSession(id="other", owner="unrelated-browser", assistant_id="assistant", title="private")); db.commit()
    import app.chat as module
    called = []
    async def should_not_allocate(*args):
        called.append(True)
        raise AssertionError("Wrong-owner request must fail before allocating HTTP clients")
    monkeypatch.setattr(module, "chat_model", should_not_allocate)
    assert client.post("/api/chat", json={"assistantId": "assistant", "sessionId": "other", "question": "继续"}).status_code == 404
    assert called == []


def test_cancel_during_retrieval_does_not_start_generation(client, db, monkeypatch):
    import time
    setup_chat(db)
    install_model(monkeypatch)
    import app.chat as module
    seen = []
    def slow_retrieve(*args):
        seen.append(True)
        time.sleep(.3)
        return []
    checks = []
    def cancel_later(_):
        checks.append(True)
        return len(checks) > 2
    monkeypatch.setattr(module, "retrieve_in_thread", slow_retrieve)
    monkeypatch.setattr(module, "cancel_requested", cancel_later)
    response = client.post("/api/chat", json={"assistantId": "assistant", "question": "取消召回"})
    parsed = events(response)
    assert parsed[-1][0] == "done" and parsed[-1][1]["status"] == "cancelled"
    assert not any(kind == "delta" for kind, _ in parsed)
    assert seen


def test_usage_summary_counts_unique_calls_and_cancels_honestly(client, db, monkeypatch):
    setup_chat(db)
    install_model(monkeypatch)
    import app.chat as module
    from app.observability import trace_context
    def retrieved_with_usage(*_):
        from app.db import SessionLocal
        trace = trace_context.get()
        with SessionLocal() as provider_db:
            provider_db.add(LogEntry(kind="模型调用", question="embedding", model="test", tokens=12, status="成功", details={"trace_id": trace["trace_id"]})); provider_db.commit()
        return []
    monkeypatch.setattr(module, "retrieve_in_thread", retrieved_with_usage)
    parsed = events(client.post("/api/chat", json={"assistantId": "assistant", "question": "no evidence"}))
    db.expire_all()
    run_id = next(data["runId"] for kind, data in parsed if kind == "meta")
    assert db.get(LogEntry, run_id).tokens == 12
    assert db.get(LogEntry, run_id).details["knownTokens"] == 12
    assert len(list(db.scalars(select(LogEntry).where(LogEntry.kind == "模型调用")))) == 1


def test_cancel_during_query_rewrite_yields_done(client, db, monkeypatch):
    import asyncio
    setup_chat(db)
    client.get("/api/health")
    own = client.cookies.get("zhixu_session")
    db.add(ChatSession(id="history", owner=own, assistant_id="assistant", title="old")); db.flush()
    db.add(ChatMessage(session_id="history", role="user", text="申请年假", status="completed")); db.commit()
    class SlowRewrite(FakeListChatModel):
        async def _agenerate(self, *args, **kwargs):
            await asyncio.sleep(10)
            raise AssertionError("Cancelled rewrite must not finish")
    import app.chat as module
    async def fake_model(*_): return SlowRewrite(responses=["unused"])
    checks = []
    def cancel_after_poll(_):
        checks.append(True)
        return len(checks) > 2
    monkeypatch.setattr(module, "chat_model", fake_model)
    monkeypatch.setattr(module, "cancel_requested", cancel_after_poll)
    parsed = events(client.post("/api/chat", json={"assistantId": "assistant", "sessionId": "history", "question": "需要提前多久"}))
    assert parsed[-1][0] == "done" and parsed[-1][1]["status"] == "cancelled"
    db.expire_all()
    call = db.scalar(select(LogEntry).where(LogEntry.kind == "模型调用"))
    assert call.details["purpose"] == "query_rewrite" and call.status == "cancelled"
    assert call.tokens is None


def test_api_startup_recovers_interrupted_chat_and_allows_continuation(client, db, monkeypatch):
    setup_chat(db)
    install_model(monkeypatch)
    client.get("/api/health")
    own = client.cookies.get("zhixu_session")
    db.add(ChatSession(id="interrupted", owner=own, assistant_id="assistant", title="中断问题")); db.flush()
    db.add(ChatMessage(id="partial-answer", session_id="interrupted", role="assistant", text="已收到的部分答案", refs=[{"name": "留存引用"}], status="running"))
    db.add(LogEntry(id="interrupted-run", kind="对话", question="上一轮", model="test", tokens=7, status="running", owner=own,
                    details={"sessionId": "interrupted", "messageId": "partial-answer", "knownTokens": 7}))
    db.add(LogEntry(kind="模型调用", question="embedding", model="test", tokens=5, status="成功", owner=own, details={"trace_id": "interrupted-run"})); db.commit()
    from fastapi.testclient import TestClient
    from app.main import app
    from app.chat import recover_interrupted_chats
    with TestClient(app) as restarted:
        restarted.cookies.set("zhixu_session", own)
        db.expire_all()
        old = db.get(LogEntry, "interrupted-run")
        assert old.status == "failed" and old.tokens is None
        assert old.details["knownTokens"] == 7 and old.details["usageComplete"] is False
        assert old.details["errorCode"] == "api_restart"
        answer = db.get(ChatMessage, "partial-answer")
        assert answer.status == "failed" and answer.text == "已收到的部分答案"
        assert answer.refs == [{"name": "留存引用"}]
        assert recover_interrupted_chats() == 0
        continuation = restarted.post("/api/chat", json={"assistantId": "assistant", "sessionId": "interrupted", "question": "重新提问"})
        assert continuation.status_code == 200
        assert next(data for kind, data in events(continuation) if kind == "done")["status"] == "completed"

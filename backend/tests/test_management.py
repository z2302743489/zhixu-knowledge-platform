from sqlalchemy import select
from app.models import ModelConfig, DocumentRecord, Assistant, ChatSession, LogEntry, Setting
from app.security import decrypt

ADMIN = {"X-Role": "admin"}
MANAGER = {"X-Role": "manager"}


def make_model(client, type="Embedding", name="知识向量"):
    r = client.post("/api/models", headers=ADMIN, json={"name": name, "provider": "OpenRouter", "type": type, "modelId": "test-model", "base": "https://openrouter.ai/api/v1", "dim": 3 if type == "Embedding" else None, "apiKey": "dummy-test-key"})
    assert r.status_code == 201, r.text
    return r.json()


def test_empty_workspace_and_role_gates(client):
    boot = client.get("/api/bootstrap").json()
    assert boot["mode"] == "live" and boot["kbs"] == [] and boot["sessions"] == []
    assert client.cookies.get("zhixu_session")
    assert client.post("/api/models", json={}).status_code == 403
    assert client.get("/api/logs", headers=MANAGER).status_code == 403
    assert client.get("/api/bootstrap", headers={"X-Role": "invented"}).status_code == 400


def test_keys_encrypted_and_blank_preserves(client, db):
    m = make_model(client)
    assert "apiKey" not in m and "encrypted_key" not in m and m["hasKey"]
    stored = db.get(ModelConfig, m["id"])
    assert "dummy-test-key" not in stored.encrypted_key and decrypt(stored.encrypted_key) == "dummy-test-key"
    r = client.patch("/api/models/" + m["id"], headers=ADMIN, json={"name": "更新名称", "apiKey": ""})
    assert r.status_code == 200 and r.json()["hasKey"]
    assert "dummy-test-key" not in client.get("/api/bootstrap", headers=ADMIN).text
    choice = client.get("/api/bootstrap", headers=MANAGER).json()["models"][0]
    assert choice["id"] == m["id"] and choice["base"] == "" and choice["modelId"] == ""


def test_embedding_identity_frozen_and_referenced_model_protected(client, db):
    m = make_model(client)
    kb = client.post("/api/kbs", headers=MANAGER, json={"name": "企业制度", "model": m["id"]}).json()
    db.add(DocumentRecord(kb_id=kb["id"], name="test.md", kind="md", status="ready", active_version="v1")); db.commit()
    assert client.patch("/api/models/" + m["id"], headers=ADMIN, json={"modelId": "new-model"}).status_code == 409
    assert client.patch("/api/models/" + m["id"], headers=ADMIN, json={"apiKey": "rotated-test-key"}).status_code == 200
    assert client.delete("/api/models/" + m["id"], headers=ADMIN).status_code == 409
    assert client.post("/api/kbs", headers=MANAGER, json={"name": "", "model": m["id"]}).status_code == 422


def test_deleted_kb_unpublishes_empty_assistant(client, db):
    embedding, llm = make_model(client), make_model(client, "LLM", "企业回答")
    kb = client.post("/api/kbs", headers=MANAGER, json={"name": "资料", "model": embedding["id"]}).json()
    a = client.post("/api/assistants", headers=MANAGER, json={"name": "企业助手", "kbIds": [kb["id"]], "model": llm["id"], "published": True}).json()
    assert a["published"]
    assert client.delete("/api/kbs/" + kb["id"], headers=MANAGER).status_code == 200
    db.expire_all(); stored = db.get(Assistant, a["id"])
    assert stored.kb_ids == [] and not stored.published


def test_anonymous_browser_session_scope(client, db):
    embedding, llm = make_model(client), make_model(client, "LLM", "问答")
    kb = client.post("/api/kbs", headers=MANAGER, json={"name": "知识", "model": embedding["id"]}).json()
    a = client.post("/api/assistants", headers=MANAGER, json={"name": "助手", "kbIds": [kb["id"]], "model": llm["id"], "published": True}).json()
    own = client.cookies.get("zhixu_session")
    db.add_all([ChatSession(owner=own, assistant_id=a["id"], title="自己的问题"), ChatSession(owner="other-browser", assistant_id=a["id"], title="他人的问题")]); db.commit()
    boot = client.get("/api/bootstrap").json()
    assert [s["title"] for s in boot["sessions"]] == ["自己的问题"]
    assert boot["models"] == [] and boot["logs"] == [] and boot["kbs"] == []


def test_ocr_secrets_and_endpoint_validation(client, db):
    v = {"region": "cn-hangzhou", "endpoint": "docmind-api.cn-hangzhou.aliyuncs.com", "accessKeyId": "test-access-id", "accessKeySecret": "test-access-secret"}
    r = client.put("/api/ocr", headers=ADMIN, json=v)
    assert r.status_code == 200 and r.json()["configured"] and "test-access" not in r.text
    assert decrypt(db.get(Setting, "ocr").value["encryptedAccessKeySecret"]) == "test-access-secret"
    assert client.put("/api/ocr", headers=ADMIN, json={**v, "endpoint": "127.0.0.1"}).status_code == 422
    assert client.put("/api/ocr", headers=ADMIN, json={"region": "cn-hangzhou"}).json()["configured"]


def test_logs_literal_filter_and_pagination(client, db):
    db.add_all([LogEntry(kind="对话", question="年假 100%", model="问答", tokens=None), LogEntry(kind="导入", question="其他资料", model="向量", tokens=12)]); db.commit()
    r = client.get("/api/logs", headers=ADMIN, params={"query": "%", "pageSize": 1}).json()
    assert r["total"] == 1 and r["items"][0]["question"] == "年假 100%"
    export = client.get("/api/logs", headers=ADMIN, params={"export": True, "kind": "导入"})
    assert len(export.json()) == 1 and "attachment" in export.headers["content-disposition"]


def test_invalid_provider_url_rejected_without_network(client):
    for url in ["https://169.254.169.254/v1", "https://user:secret@api.example.com/v1"]:
        r = client.post("/api/models", headers=ADMIN, json={"name": "test", "provider": "OpenRouter", "type": "LLM", "modelId": "test", "base": url})
        assert r.status_code == 422
def test_validation_never_echoes_rejected_credentials(client):
    secret = "private-fixture-credential-" * 100
    result = client.post('/api/models', headers={'X-Role': 'admin'}, json={
        'name': 'Validation', 'provider': 'OpenRouter', 'type': 'LLM',
        'modelId': 'fixture', 'base': 'https://openrouter.ai/api/v1', 'apiKey': secret})
    assert result.status_code == 422
    assert secret not in result.text and 'private-fixture-credential-' not in result.text

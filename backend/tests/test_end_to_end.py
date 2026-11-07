"""Real application path against a local HTTP provider fixture, never product mocks."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from app.models import LogEntry
from sqlalchemy import select


@pytest.fixture
def provider_server():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((self.path, body))
            self.send_response(200)
            if self.path.endswith('/embeddings'):
                self.send_header('Content-Type', 'application/json'); self.end_headers()
                values = [{'index': i, 'embedding': [1.0 if '年假' in text else .1, 1.0 if '体检' in text else .1, .1]}
                          for i, text in enumerate(body['input'])]
                self.wfile.write(json.dumps({'data': values, 'usage': {'total_tokens': 12}}).encode())
            elif self.path.endswith('/rerank'):
                self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'results': [{'index': i, 'relevance_score': 1.0 / (i+1)} for i in range(len(body['documents']))],
                                             'usage': {'total_tokens': 8}}).encode())
            else:
                self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json'); self.end_headers()
                if not body.get('stream'):
                    self.wfile.write(json.dumps({'id': 'query', 'model': 'fixture', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '年假申请流程'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}).encode())
                else:
                    for delta in ['年假申请需要', '提前三天，', '通过 OA 审批。[1]']:
                        payload = {'id': 'answer', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                                   'choices': [{'index': 0, 'delta': {'content': delta}, 'finish_reason': None}]}
                        self.wfile.write(('data: '+json.dumps(payload, ensure_ascii=False)+'\n\n').encode())
                    self.wfile.write(b'data: {"id":"answer","object":"chat.completion.chunk","created":1,"model":"fixture","choices":[],"usage":{"prompt_tokens":20,"completion_tokens":10,"total_tokens":30}}\n\ndata: [DONE]\n\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}/v1', requests
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_upload_index_rerank_chat_and_real_usage(client, db, provider_server):
    base, calls = provider_server
    admin = {'X-Role': 'admin'}
    assert client.get('/api/bootstrap', headers=admin).json()['kbs'] == []
    def model(name, kind, **extra):
        response = client.post('/api/models', headers=admin, json={'name': name, 'provider': 'AIHubMix', 'type': kind,
                               'modelId': 'fixture', 'base': base, 'apiKey': 'test-only-token', **extra})
        assert response.status_code == 201, response.text
        assert 'test-only-token' not in response.text
        return response.json()['id']
    embed = model('Embedding fixture', 'Embedding', dim=3)
    llm = model('LLM fixture', 'LLM')
    model('Rerank fixture', 'Rerank')
    library = client.post('/api/kbs', headers=admin, json={'name': 'End to end', 'model': embed}).json()['id']
    upload = client.post('/api/kbs/'+library+'/documents', headers=admin, files={'files': ('policy.md', '# 年假制度\n年假申请需要提前三天，通过 OA 审批。'.encode())}, data={'options':'{"autoBuild":false,"cloudParser":false}'})
    assert upload.status_code == 200, upload.text
    document_id = upload.json()['documents'][0]['id']
    assert client.get('/api/jobs/'+upload.json()['jobs'][0]['id'], headers=admin).json()['status'] == 'preview'
    build = client.post('/api/documents/'+document_id+'/build', headers=admin)
    assert build.status_code == 200
    job = client.get('/api/jobs/'+build.json()['jobId'], headers=admin).json()
    assert job['status'] == 'completed', job
    search = client.post('/api/kbs/'+library+'/search', headers=admin, json={'question':'年假申请', 'mode':'hybrid','rerank':True})
    assert search.status_code == 200 and search.json()['results'][0]['doc']['id'] == document_id, search.text
    assistant = client.post('/api/assistants', headers=admin, json={'name':'HR','model':llm,'kbIds':[library],'rerank':True,'published':True}).json()['id']
    answer = client.post('/api/chat', json={'assistantId':assistant,'question':'年假怎么申请'})
    assert answer.status_code == 200 and '通过 OA 审批' in answer.text, answer.text
    assert '"total_tokens": 30' not in answer.text  # Usage stays in logs, not synthesized into answer.
    summary = client.get('/api/bootstrap', headers=admin).json()
    assert summary['sessions'][0]['messages'][-1]['refs'][0]['documentId'] == document_id
    db.expire_all()
    run = db.scalar(select(LogEntry).where(LogEntry.kind=='对话'))
    assert run.tokens == 50, run.details
    assert run.status == 'completed'
    assert any(path.endswith('/embeddings') for path, _ in calls)
    assert any(path.endswith('/rerank') for path, _ in calls)
    assert any(path.endswith('/chat/completions') for path, _ in calls)
    # Immediately invalidating knowledge prevents it being served even while vectors remain.
    assert client.patch('/api/documents/'+document_id, headers=admin, json={'enabled':False}).status_code == 200
    assert client.post('/api/kbs/'+library+'/search', headers=admin, json={'question':'年假','rerank':False}).json()['results'] == []

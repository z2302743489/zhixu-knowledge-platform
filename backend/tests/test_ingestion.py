import io
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from app.models import ChunkRecord, DocumentRecord, ImportJob, KnowledgeBase, ModelConfig
from app.parsers import ParseError, parse_file, split_documents, validate_file


def create_kb(db):
    model = ModelConfig(id='embedding-test', name='Embedding', provider='AIHubMix', type='Embedding', model_id='embed', base='https://example.com/v1', dimension=3, enabled=True)
    db.add(model)
    db.flush()
    kb = KnowledgeBase(id='kb-test', name='Knowledge', model_id=model.id)
    db.add(kb)
    db.commit()
    return kb


def test_markdown_and_table_source_locations(tmp_path):
    path = tmp_path / 'policy.md'
    path.write_text('# 年假\n申请需提前三天。\n\n## 流程\n负责人审批。', encoding='utf-8')
    docs = parse_file(path, path.name, cloud_parser=False)
    assert docs[0].metadata['location'] == '年假 / 第 1–3 行'
    assert '流程' in docs[1].metadata['location']
    assert all(d.metadata.get('location') for d in split_documents(docs))
    csv = tmp_path / 'benefit.csv'
    csv.write_text('福利,说明\n体检,每年一次\n基金,审批后报销', encoding='utf-8')
    docs = parse_file(csv, csv.name, cloud_parser=False)
    assert '| 福利 | 说明 |' in docs[0].page_content
    assert '2–3' in docs[0].metadata['location']


def test_office_native_parsing(tmp_path):
    from docx import Document
    from openpyxl import Workbook
    word = Document()
    word.add_heading('入职', 1)
    word.add_paragraph('携带证件。')
    table = word.add_table(rows=2, cols=2)
    table.cell(0, 0).text = '项目'
    table.cell(0, 1).text = '说明'
    table.cell(1, 0).text = '设备'
    table.cell(1, 1).text = '前台领取'
    path = tmp_path / 'guide.docx'
    word.save(path)
    docs = parse_file(path, path.name)
    assert any('入职 / 段落' in d.metadata['location'] for d in docs)
    assert any('前台领取' in d.page_content and d.metadata['table'] for d in docs)
    book = Workbook()
    book.active.title = '福利'
    book.active.append(['项目', '次数'])
    book.active.append(['体检', 1])
    sheet = tmp_path / 'benefits.xlsx'
    book.save(sheet)
    result = parse_file(sheet, sheet.name)
    assert '福利' in result[0].metadata['location']
    assert '体检' in result[0].page_content


def test_reject_mismatched_or_empty_binary(tmp_path):
    invalid = tmp_path / 'fake.pdf'
    invalid.write_bytes(b'<html>untrusted</html>')
    with pytest.raises(ParseError, match='扩展名'):
        validate_file(invalid, invalid.name)
    invalid.write_bytes(b'')
    with pytest.raises(ParseError, match='文件为空'):
        validate_file(invalid, invalid.name)


def test_upload_preview_dedup_and_disabled_filter(client, db):
    create_kb(db)
    headers = {'X-Role': 'manager'}
    response = client.post('/api/kbs/kb-test/documents', headers=headers, files={'files': ('guide.md', '# Guide\n年假申请需要负责人审批。'.encode(), 'text/markdown')}, data={'options': json.dumps({'autoBuild': False, 'cloudParser': False})})
    assert response.status_code == 200, response.text
    document_id = response.json()['documents'][0]['id']
    job_id = response.json()['jobs'][0]['id']
    job = client.get('/api/jobs/' + job_id, headers=headers).json()
    assert job['status'] == 'preview'
    chunks = client.get(f'/api/documents/{document_id}/chunks?preview=true', headers=headers).json()['items']
    assert chunks and '负责人审批' in chunks[0]['text']
    duplicate = client.post('/api/kbs/kb-test/documents', headers=headers, files={'files': ('renamed.md', '# Guide\n年假申请需要负责人审批。'.encode(), 'text/markdown')})
    assert duplicate.json()['documents'][0]['duplicate'] is True
    assert duplicate.json()['documents'][0]['id'] == document_id
    assert client.get(f'/api/documents/{document_id}/file', headers=headers).content.startswith(b'# Guide')
    assert client.patch(f'/api/documents/{document_id}', headers=headers, json={'enabled': False}).json()['status'] == 'disabled'
    assert client.post(f'/api/documents/{document_id}/build', headers=headers).status_code == 409
    assert client.delete(f'/api/documents/{document_id}', headers=headers).json()['ok']
    assert client.get(f'/api/documents/{document_id}/chunks', headers=headers).status_code == 404


def test_partial_batch_does_not_fail_valid_file(client, db):
    create_kb(db)
    response = client.post('/api/kbs/kb-test/documents', headers={'X-Role': 'admin'}, files=[('files', ('bad.pdf', b'not pdf', 'application/pdf')), ('files', ('valid.md', b'# Valid\nReal data', 'text/plain'))], data={'options': '{"autoBuild":false,"cloudParser":false}'})
    assert response.status_code == 200
    assert len(response.json()['documents']) == 1
    assert response.json()['errors'][0]['name'] == 'bad.pdf'


def test_cloud_job_resume_and_layout_pagination(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import app.parsers as parsers
    submitted = []
    class Cloud:
        def submit_doc_parser_job_advance(self, request, runtime):
            submitted.append(request.file_name)
            return SimpleNamespace(body=SimpleNamespace(data={'Id': 'remote-123'}))
        def query_doc_parser_status(self, request):
            return SimpleNamespace(body=SimpleNamespace(data={'Status': 'success'}))
        def get_doc_parser_result(self, request):
            count = 100 if request.layout_num == 0 else 1
            layouts = [{'text': f'内容 {request.layout_num + i}', 'index': request.layout_num + i, 'pageNum': 2} for i in range(count)]
            return SimpleNamespace(body=SimpleNamespace(data={'layouts': layouts}))
    monkeypatch.setattr(parsers, '_cloud_client', lambda _: Cloud())
    path = tmp_path / 'scan.pdf'
    path.write_bytes(b'%PDF-valid-test-placeholder')
    states = []
    result = parsers.cloud_parse(path, 'scan.pdf', {}, {}, states.append)
    assert len(result) == 101
    assert '第 2 页' in result[0].metadata['location']
    assert states[0]['id'] == 'remote-123'
    # Resuming a saved provider job never submits the file again.
    resumed = parsers.cloud_parse(path, 'scan.pdf', {}, {'id': 'remote-123'}, states.append)
    assert len(resumed) == 101
    assert submitted == ['scan.pdf']


def test_replace_file_keeps_original_version(client, db, monkeypatch):
    from langchain_core.embeddings import Embeddings
    class TestEmbeddings(Embeddings):
        def embed_documents(self, texts):
            return [[1., .1, .1] for _ in texts]
        def embed_query(self, text):
            return [1., .1, .1]
    import app.providers
    monkeypatch.setattr(app.providers, 'embedding_model', lambda *_: TestEmbeddings())
    create_kb(db)
    headers = {'X-Role': 'admin'}
    original = '# Policy\n年假申请提前三天。'.encode()
    uploaded = client.post('/api/kbs/kb-test/documents', headers=headers, files={'files': ('policy.md', original, 'text/markdown')}, data={'options': '{"cloudParser":false}'}).json()
    document_id = uploaded['documents'][0]['id']
    before = client.get('/api/documents/' + document_id + '/chunks', headers=headers).json()
    old_version = before['version']
    updated = '# Policy\n年假申请提前五天。'.encode()
    response = client.post('/api/documents/' + document_id + '/versions', headers=headers, files={'file': ('policy-v2.md', updated, 'text/markdown')}, data={'options': '{"cloudParser":false}'} )
    assert response.status_code == 200, response.text
    assert response.json()['document']['id'] == document_id
    assert client.get('/api/jobs/' + response.json()['jobId'], headers=headers).json()['status'] == 'completed'
    after = client.get('/api/documents/' + document_id + '/chunks', headers=headers).json()
    assert after['version'] != old_version
    assert '五天' in after['items'][0]['text']
    assert client.get('/api/documents/' + document_id + '/file?version=' + old_version, headers=headers).content == original
    assert client.get('/api/documents/' + document_id + '/file', headers=headers).content == updated


def test_upload_aggregate_budget_preserves_earlier_valid_file(client, db, monkeypatch):
    import app.routes_documents
    monkeypatch.setattr(app.routes_documents, 'MAX_FILE_BYTES', 4096)
    create_kb(db)
    response = client.post('/api/kbs/kb-test/documents', headers={'X-Role': 'admin'}, files=[('files', ('first.md', b'# First\n' + b'a' * 3000, 'text/plain')), ('files', ('second.md', b'# Second\n' + b'b' * 3000, 'text/plain'))], data={'options': '{"autoBuild":false,"cloudParser":false}'})
    assert response.status_code == 200
    assert len(response.json()['documents']) == 1
    assert response.json()['documents'][0]['name'] == 'first.md'
    assert '单次上传总量' in response.json()['errors'][0]['error']


def test_live_hash_constraint_across_database_sessions(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    create_kb(db)
    barrier = Barrier(2)
    def insert(name):
        with SessionLocal() as session:
            candidate = DocumentRecord(kb_id='kb-test', name=name, kind='md', status='preview', sha256='a' * 64)
            session.add(candidate)
            barrier.wait(timeout=5)
            try:
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(insert, ['left.md', 'right.md']))
    assert sorted(outcomes) == [False, True]
    rows = db.scalars(select(DocumentRecord).where(DocumentRecord.sha256 == 'a' * 64)).all()
    assert len(rows) == 1
    # Deleted originals no longer prevent a fresh import of identical bytes.
    rows[0].status = 'deleted'
    db.commit()
    db.add(DocumentRecord(kb_id='kb-test', name='reimport.md', kind='md', sha256='a' * 64))
    db.commit()


def test_upload_constraint_conflict_returns_existing_file(client, db, monkeypatch):
    from sqlalchemy.orm import Session
    from app.db import SessionLocal
    create_kb(db)
    original_flush = Session.flush
    race_fired = []
    def racing_flush(session, objects=None):
        candidate = next((value for value in session.new if isinstance(value, DocumentRecord) and value.name == 'race.md'), None)
        if candidate and not race_fired:
            race_fired.append(True)
            with SessionLocal() as other:
                other.add(DocumentRecord(id='race-winner', kb_id=candidate.kb_id, name='winning-upload.md', kind=candidate.kind, sha256=candidate.sha256, status='preview'))
                original_flush(other)
                other.commit()
        return original_flush(session, objects)
    monkeypatch.setattr(Session, 'flush', racing_flush)
    response = client.post('/api/kbs/kb-test/documents', headers={'X-Role': 'admin'}, files={'files': ('race.md', b'# Race\nSame file bytes', 'text/plain')}, data={'options': '{"autoBuild":false,"cloudParser":false}'})
    assert response.status_code == 200, response.text
    assert response.json()['documents'][0]['id'] == 'race-winner'
    assert response.json()['documents'][0]['duplicate'] is True


def test_replace_with_another_documents_content_returns_conflict(client, db):
    create_kb(db)
    headers = {'X-Role': 'admin'}
    def upload(name, content):
        response = client.post('/api/kbs/kb-test/documents', headers=headers, files={'files': (name, content, 'text/plain')}, data={'options': '{"autoBuild":false,"cloudParser":false}'})
        return response.json()['documents'][0]['id']
    first = upload('first.md', b'# First\nOne')
    second = upload('second.md', b'# Second\nTwo')
    response = client.post('/api/documents/' + second + '/versions', headers=headers, files={'file': ('duplicate.md', b'# First\nOne', 'text/plain')})
    assert response.status_code == 409
    assert '相同内容' in response.json()['detail']

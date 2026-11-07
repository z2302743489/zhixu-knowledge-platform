"""Exercise a DISPOSABLE local Compose deployment without external AI credentials.

Creates test records/files. CI destroys its volumes afterward. It verifies nginx,
PostgreSQL migrations, Celery/Redis parsing, previews, source storage, and Qdrant
connectivity; genuine model/OCR calls are intentionally outside this smoke check.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.base_url)
    if parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise SystemExit('Smoke checks only target a disposable local HTTP deployment.')
    base = args.base_url.rstrip('/')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, *, method='GET', data=None, content_type='application/json', role='admin'):
        raw = json.dumps(data).encode() if data is not None and not isinstance(data, bytes) else data
        req = urllib.request.Request(base + path, data=raw, method=method, headers={'Content-Type': content_type, 'X-Role': role})
        with opener.open(req, timeout=20) as response:
            payload = response.read()
            return json.loads(payload) if 'application/json' in response.headers.get('Content-Type', '') else payload

    health = request('/api/health')
    assert health['status'] == 'ok' and health['taskMode'] == 'celery', health
    assert b'<html' in request('/')
    model = request('/api/models', method='POST', data={'name': 'CI preview embedding configuration', 'provider': 'OpenRouter', 'type': 'Embedding', 'modelId': 'configure-after-deploy', 'base': 'https://openrouter.ai/api/v1', 'dim': 3})
    kb = request('/api/kbs', method='POST', data={'name': 'CI disposable native parsing', 'model': model['id']})
    text = '# CI validation\nNative Markdown content must reach persistent parsed chunks.\n'
    boundary = 'zhixu-' + uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="options"\r\n\r\n{{"autoBuild":false,"cloudParser":false}}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="ci-check.md"\r\nContent-Type: text/markdown\r\n\r\n{text}\r\n'
            f'--{boundary}--\r\n').encode()
    upload = request('/api/kbs/' + kb['id'] + '/documents', method='POST', data=body, content_type='multipart/form-data; boundary=' + boundary)
    job_id, doc_id = upload['jobs'][0]['id'], upload['documents'][0]['id']
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        job = request('/api/jobs/' + job_id)
        if job['status'] in {'preview', 'failed'}:
            break
        time.sleep(.5)
    else:
        raise AssertionError('Celery did not process the native parsing job in time')
    assert job['status'] == 'preview', job
    chunks = request('/api/documents/' + doc_id + '/chunks?preview=true')
    assert chunks['items'] and 'persistent parsed chunks' in chunks['items'][0]['text'], chunks
    assert request('/api/documents/' + doc_id + '/file') == text.encode()
    try:
        request('/api/logs', role='user')
    except urllib.error.HTTPError as error:
        assert error.code == 403
    else:
        raise AssertionError('User operation mode unexpectedly accessed global logs')
    request('/api/kbs/' + kb['id'], method='DELETE')
    request('/api/models/' + model['id'], method='DELETE')
    print('Compose smoke passed: frontend/API, SQL persistence, Celery native parse, preview/source, role gates.')


if __name__ == '__main__':
    main()

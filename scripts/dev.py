"""Start the local API and Vite; terminate both children on Ctrl-C."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

root = Path(__file__).resolve().parents[1]
python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
if not python.exists() or not (root / 'node_modules').exists():
    raise SystemExit('Install Python requirements into .venv and run npm ci first. See README.md.')
port = os.environ.get('ZHIXU_API_PORT', '8010')
env = {**os.environ, 'ZHIXU_API_URL': f'http://127.0.0.1:{port}'}
children = []
def cleanup():
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    for child in children:
        try: child.wait(timeout=8)
        except subprocess.TimeoutExpired: child.kill()
def stop(*_):
    cleanup()
    raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
try:
    subprocess.run([str(python), '-m', 'alembic', '-c', 'backend/alembic.ini', 'upgrade', 'head'], cwd=root, env=env, check=True)
    children.append(subprocess.Popen([str(python), '-m', 'uvicorn', 'app.main:app', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', port], cwd=root, env=env))
    for _ in range(60):
        if children[0].poll() is not None:
            raise SystemExit('API did not start; inspect the error above.')
        try:
            with urllib.request.urlopen(env['ZHIXU_API_URL'] + '/api/health', timeout=1) as result:
                if result.status == 200: break
        except OSError: time.sleep(.25)
    else: raise SystemExit('API startup timed out.')
    children.append(subprocess.Popen(['npm.cmd' if os.name == 'nt' else 'npm', 'run', 'dev'], cwd=root, env=env))
    while all(child.poll() is None for child in children): time.sleep(.5)
finally:
    cleanup()

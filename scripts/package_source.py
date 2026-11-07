"""Create a source-only GitHub handoff ZIP using an explicit allowlist."""
from pathlib import Path
import hashlib
import json
import zipfile

root = Path(__file__).resolve().parents[1]
target = root.parent / 'zhixu-knowledge-platform-v1.0.0-source.zip'
folders = ('src', 'backend', 'scripts', 'examples', 'docs', '.github', 'screenshots')
files = {'.env.example', '.gitignore', '.dockerignore', 'Dockerfile', 'compose.yaml', 'nginx.conf', 'index.html', 'package.json', 'package-lock.json', 'tsconfig.json', 'vite.config.ts', 'README.md', 'SECURITY.md', 'LICENSE', '验收记录.md'}
excluded = {'__pycache__', '.pytest_cache', 'data', '.venv', 'node_modules', '.git'}
entries = []
for path in sorted(root.rglob('*')):
    if not path.is_file(): continue
    relative = path.relative_to(root)
    if any(part in excluded for part in relative.parts): continue
    if relative.parts[0] not in folders and relative.as_posix() not in files: continue
    if path.suffix in {'.pyc', '.db', '.log', '.zip'}: continue
    if path.name.startswith('.env') and path.name != '.env.example': continue
    entries.append((path, relative))
manifest = {str(relative): hashlib.sha256(path.read_bytes()).hexdigest() for path, relative in entries}
with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for path, relative in entries:
        archive.write(path, 'zhixu-knowledge-platform/' + relative.as_posix())
    archive.writestr('zhixu-knowledge-platform/SOURCE_MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
with zipfile.ZipFile(target) as archive:
    assert archive.testzip() is None
    assert not any('/data/' in name or '/.env' == name[-5:] or '/node_modules/' in name for name in archive.namelist())
print(f'Source archive: {target.name}; {len(entries)} files; {target.stat().st_size} bytes')
print('SHA256:', hashlib.sha256(target.read_bytes()).hexdigest())

"""Create ignored local deployment secrets without printing them."""
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
target = root / '.env'
if target.exists():
    raise SystemExit('.env already exists; left unchanged.')
with target.open('x') as handle:
    handle.write('POSTGRES_PASSWORD=' + secrets.token_hex(24) + '\n')
    handle.write('ZHIXU_SECRET_KEY=' + secrets.token_hex(32) + '\n')
    handle.write('ZHIXU_COOKIE_SECURE=false\nZHIXU_ALLOWED_PRIVATE_HOSTS=\nZHIXU_BIND_ADDRESS=127.0.0.1\nZHIXU_PORT=8080\n')
target.chmod(0o600)
print('Created .env with new local secrets. Keep this file out of GitHub.')

#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
COMPOSE="docker compose -f deploy/docker-compose.yml"
BASE_URL=${NESTRA_BASE_URL-http://127.0.0.1:8080}
BASE_URL_EXPLICIT=${NESTRA_BASE_URL+x}

command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker daemon is unavailable" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose is required" >&2; exit 1; }
available_kb=$(df -Pk "$ROOT" | awk 'NR == 2 {print $4}')
[ "${available_kb:-0}" -ge 2097152 ] || {
    echo "At least 2 GiB of free disk space is required" >&2
    exit 1
}

mkdir -p data/db data/attachments data/models/tagsets data/backups config
chmod 0700 data data/db data/attachments data/models data/models/tagsets data/backups
configure_base_url=0
if [ ! -f config/config.yaml ]; then
    cp config/config.example.yaml config/config.yaml
    configure_base_url=1
fi
[ "$BASE_URL_EXPLICIT" != x ] || configure_base_url=1
if [ "$configure_base_url" -eq 1 ]; then
    python3 - "$BASE_URL" <<'PY'
import sys
from pathlib import Path
from urllib.parse import urlsplit
url=sys.argv[1].rstrip('/')
parsed=urlsplit(url)
if (
    parsed.scheme not in {'http','https'}
    or not parsed.hostname
    or parsed.username is not None
    or parsed.password is not None
    or parsed.query
    or parsed.fragment
    or parsed.path not in {'', '/'}
    or any(char.isspace() or ord(char) < 32 for char in url)
):
    raise SystemExit('NESTRA_BASE_URL must be an http(s) origin without credentials or path')
p=Path('config/config.yaml')
lines=p.read_text().splitlines()
in_web=False
for i,line in enumerate(lines):
    if line and not line.startswith(' '): in_web=line.rstrip()== 'web:'
    if in_web and line.lstrip().startswith('base_url:'):
        lines[i]=f'  base_url: {url}'
    elif in_web and line.lstrip().startswith('cookie_secure:'):
        lines[i]=f"  cookie_secure: {'true' if parsed.scheme == 'https' else 'false'}"
p.write_text('\n'.join(lines)+'\n')
PY
else
    BASE_URL=$(python3 - <<'PY'
from pathlib import Path
inside=False
for line in Path('config/config.yaml').read_text().splitlines():
    if line and not line.startswith(' '): inside=line.rstrip() == 'web:'
    elif inside and line.lstrip().startswith('base_url:'):
        print(line.split(':', 1)[1].split('#', 1)[0].strip().strip("'\""))
        break
PY
)
    [ -n "$BASE_URL" ] || { echo "web.base_url is missing" >&2; exit 1; }
fi

if [ ! -f .env ]; then
    umask 077
    cp config/env.example .env
    python3 - <<'PY'
import os, secrets
from pathlib import Path
p=Path('.env')
values={
    'NESTRA_SECRET_KEY': secrets.token_urlsafe(48),
    'NESTRA_UID': str(os.getuid() or 1000),
    'NESTRA_GID': str(os.getgid() or 1000),
}
for name in ('DEEPSEEK_API_KEY','GEMINI_API_KEY','OPENROUTER_API_KEY','ANTHROPIC_API_KEY','NESTRA_ADMIN_PASSWORD'):
    if os.environ.get(name): values[name]=os.environ[name]
lines=p.read_text().splitlines()
p.write_text('\n'.join(f"{k}={values.get(k, v)}" if (k:=line.partition('=')[0]) in values else line for line in lines for v in [line.partition('=')[2]])+'\n')
PY
    echo "Created .env with a random application key. Back it up securely."
fi
current_secret=$(sed -n 's/^NESTRA_SECRET_KEY=//p' .env | tail -1)
if [ "${#current_secret}" -lt 32 ]; then
    secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
    python3 - "$secret" <<'PY'
import sys
from pathlib import Path
path=Path('.env')
secret=sys.argv[1]
lines=path.read_text().splitlines()
updated=[]
found=False
for line in lines:
    if line.startswith('NESTRA_SECRET_KEY='):
        if not found:
            updated.append(f'NESTRA_SECRET_KEY={secret}')
            found=True
    else:
        updated.append(line)
if not found:
    updated.append(f'NESTRA_SECRET_KEY={secret}')
path.write_text('\n'.join(updated)+'\n')
PY
    echo "Generated missing NESTRA_SECRET_KEY in .env. Back it up securely."
fi
chmod 0600 .env

# Root-owned checkouts still run the container as UID/GID 1000.
uid=$(sed -n 's/^NESTRA_UID=//p' .env | tail -1)
gid=$(sed -n 's/^NESTRA_GID=//p' .env | tail -1)
uid=${uid:-1000}; gid=${gid:-1000}
[ "$uid" -gt 0 ] && [ "$gid" -gt 0 ] || { echo "NESTRA_UID/GID must be non-zero" >&2; exit 1; }
if [ "$(id -u)" -eq 0 ]; then chown -R "$uid:$gid" data; fi

if ! grep -Eq '^(DEEPSEEK_API_KEY|GEMINI_API_KEY|OPENROUTER_API_KEY|ANTHROPIC_API_KEY)=.+' .env; then
    echo "Warning: no LLM key is set; crawling works, but tagging waits until one is configured." >&2
fi

$COMPOSE config --quiet
$COMPOSE up --build -d

for _ in $(seq 1 90); do
    state=$($COMPOSE ps --format json 2>/dev/null || true)
    if echo "$state" | grep -q '"Health":"healthy"'; then
        logs=$($COMPOSE logs --no-color --tail 200 nestra 2>/dev/null || true)
        setup=$(printf '%s\n' "$logs" | python3 -c '
import json, sys
token = ""
for line in sys.stdin:
    try:
        event = json.loads(line[line.index("{"):])
    except (ValueError, json.JSONDecodeError):
        continue
    if event.get("event") == "initial_admin_setup_required":
        token = event.get("setup_token", "")
print(token)
')
        echo "Nestra is healthy: $BASE_URL"
        [ -z "$setup" ] || echo "Initial administrator setup: $BASE_URL/setup?token=$setup"
        exit 0
    fi
    sleep 1
done
$COMPOSE logs --tail 100 nestra >&2
exit 1

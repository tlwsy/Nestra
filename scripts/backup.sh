#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
umask 077
mkdir -p data/backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out=${1:-"data/backups/nestra-$stamp-$$.tar.gz"}
[ ! -e "$out" ] || { echo "backup already exists: $out" >&2; exit 1; }
include_attachments=${INCLUDE_ATTACHMENTS:-1}

python3 - "$out" "$include_attachments" <<'PY'
import json, os, sqlite3, sys, tarfile, tempfile
from pathlib import Path
out=Path(sys.argv[1]).resolve(); include=sys.argv[2]=='1'
src=Path('data/db/nestra.db')
if not src.is_file(): raise SystemExit('database not found: data/db/nestra.db')
out.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(dir=out.parent) as td:
    stage=Path(td)
    db=stage/'nestra.db'
    with sqlite3.connect(src) as source, sqlite3.connect(db) as target:
        source.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise SystemExit('backup integrity check failed')
    (stage/'manifest.json').write_text(json.dumps({'version':1,'attachments':include})+'\n')
    with tarfile.open(out, 'w:gz') as tar:
        tar.add(db, arcname='db/nestra.db')
        tar.add(stage/'manifest.json', arcname='manifest.json')
        for path, arc in ((Path('config/config.yaml'),'config/config.yaml'),(Path('.env'),'.env')):
            if path.is_file(): tar.add(path, arcname=arc)
        if include and Path('data/attachments').is_dir():
            tar.add('data/attachments', arcname='attachments')
        if Path('data/models').is_dir():
            tar.add('data/models', arcname='models')
os.chmod(out, 0o600)
print(out)
PY

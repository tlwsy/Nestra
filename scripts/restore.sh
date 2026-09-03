#!/bin/sh
set -eu
[ "$#" -eq 1 ] || { echo "usage: $0 BACKUP.tar.gz" >&2; exit 2; }
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
umask 077
archive=$(realpath "$1")
[ -f "$archive" ] || { echo "backup not found: $archive" >&2; exit 1; }
COMPOSE="docker compose -f deploy/docker-compose.yml"
stage=$(mktemp -d)
recovery=
replacement_started=0
service_stopped=0

owner_data() {
    uid=$(sed -n 's/^NESTRA_UID=//p' .env 2>/dev/null | tail -1); uid=${uid:-1000}
    gid=$(sed -n 's/^NESTRA_GID=//p' .env 2>/dev/null | tail -1); gid=${gid:-1000}
    [ "$(id -u)" -ne 0 ] || chown -R "$uid:$gid" data
}

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$replacement_started" -eq 1 ]; then
        set +e
        $COMPOSE stop nestra >/dev/null 2>&1
        if [ -f "$stage/current.db" ]; then
            install -m 0600 "$stage/current.db" data/db/nestra.db
        else
            rm -f data/db/nestra.db
        fi
        rm -f data/db/nestra.db-wal data/db/nestra.db-shm
        if [ -f "$stage/current.config.yaml" ]; then
            install -m 0644 "$stage/current.config.yaml" config/config.yaml
        else
            rm -f config/config.yaml
        fi
        if [ -f "$stage/current.env" ]; then
            install -m 0600 "$stage/current.env" .env
        else
            rm -f .env
        fi
        for directory in attachments models; do
            if [ -d "data/$directory.pre-restore" ]; then
                rm -rf "data/$directory"
                mv "data/$directory.pre-restore" "data/$directory"
            fi
        done
        owner_data
        $COMPOSE up -d >/dev/null 2>&1
        printf '%s\n' "Restore failed and previous files were restored. Recovery backup: ${recovery:-none}" >&2
    elif [ "$service_stopped" -eq 1 ]; then
        set +e
        $COMPOSE up -d >/dev/null 2>&1
    fi
    rm -rf "$stage"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# Validate and stage the complete archive before stopping the service.
python3 - "$archive" "$stage" <<'PY'
import sqlite3, sys, tarfile
from pathlib import Path, PurePosixPath
archive,stage=Path(sys.argv[1]),Path(sys.argv[2])
allowed={'.env','manifest.json','config/config.yaml','db/nestra.db','attachments','models'}
with tarfile.open(archive) as tar:
    for member in tar.getmembers():
        name=member.name.rstrip('/')
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts:
            raise SystemExit(f'unsafe backup member: {name}')
        if name not in allowed and not name.startswith(('attachments/','models/')):
            raise SystemExit(f'unexpected backup member: {name}')
        if not (member.isfile() or member.isdir()):
            raise SystemExit('special files and links are forbidden in backups')
    tar.extractall(stage)  # members were strictly allowlisted above
db=stage/'db/nestra.db'
if not db.is_file(): raise SystemExit('backup has no database')
with sqlite3.connect(db) as con:
    if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit('backup database failed integrity_check')
PY

$COMPOSE stop nestra >/dev/null
[ -z "$($COMPOSE ps --status running -q nestra)" ] || {
    echo "refusing to replace a running database" >&2
    exit 1
}
service_stopped=1
# The recovery archive is consistent because the service is stopped.
if [ -f data/db/nestra.db ]; then recovery=$(./scripts/backup.sh); fi
[ ! -f data/db/nestra.db ] || cp -p data/db/nestra.db "$stage/current.db"
[ ! -f config/config.yaml ] || cp -p config/config.yaml "$stage/current.config.yaml"
[ ! -f .env ] || cp -p .env "$stage/current.env"
replacement_started=1

install -d -m 0700 data data/db
install -m 0600 "$stage/db/nestra.db" data/db/nestra.db
rm -f data/db/nestra.db-wal data/db/nestra.db-shm
[ ! -f "$stage/config/config.yaml" ] || install -m 0644 "$stage/config/config.yaml" config/config.yaml
[ ! -f "$stage/.env" ] || install -m 0600 "$stage/.env" .env
for directory in attachments models; do
    if [ -d "$stage/$directory" ]; then
        rm -rf "data/$directory.pre-restore"
        [ ! -d "data/$directory" ] || mv "data/$directory" "data/$directory.pre-restore"
        install -d -m 0700 "data/$directory"
        cp -a "$stage/$directory/." "data/$directory/"
    fi
done
owner_data
$COMPOSE up -d
for _ in $(seq 1 90); do
    state=$($COMPOSE ps --format json 2>/dev/null || true)
    if echo "$state" | grep -q '"Health":"healthy"'; then
        replacement_started=0
        service_stopped=0
        rm -rf data/attachments.pre-restore data/models.pre-restore
        printf '%s\n' "Restore complete: $archive"
        exit 0
    fi
    sleep 1
done
$COMPOSE logs --tail 100 nestra >&2 || true
exit 1

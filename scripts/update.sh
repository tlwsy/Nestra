#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
COMPOSE="docker compose -f deploy/docker-compose.yml"

old_commit=
if old_commit=$(git rev-parse HEAD 2>/dev/null); then
    [ -z "$(git status --porcelain --untracked-files=no)" ] || {
        echo "Tracked changes present; refusing update" >&2; exit 1;
    }
    git pull --ff-only
fi

had_old=0
if docker image inspect nestra:local >/dev/null 2>&1; then
    docker tag nestra:local nestra:rollback
    had_old=1
fi

# Build while the old container is still serving. Stop only for the final,
# current database backup so rollback cannot discard writes made during a long build.
if ! $COMPOSE build; then
    [ -z "$old_commit" ] || git reset --hard "$old_commit"
    [ "$had_old" -eq 0 ] || docker tag nestra:rollback nestra:local
    echo "Update build failed; the old service was left running" >&2
    exit 1
fi
$COMPOSE stop nestra
if ! backup=$(./scripts/backup.sh); then
    [ -z "$old_commit" ] || git reset --hard "$old_commit"
    [ "$had_old" -eq 0 ] || docker tag nestra:rollback nestra:local
    $COMPOSE up -d >/dev/null 2>&1 || true
    echo "Update backup failed; the old service was restarted" >&2
    exit 1
fi

if $COMPOSE up -d; then
    for _ in $(seq 1 90); do
        state=$($COMPOSE ps --format json 2>/dev/null || true)
        echo "$state" | grep -q '"Health":"healthy"' && {
            docker image rm nestra:rollback >/dev/null 2>&1 || true
            echo "Update complete; backup: $backup"
            exit 0
        }
        sleep 1
    done
fi

$COMPOSE logs --tail 100 nestra >&2 || true
$COMPOSE down >/dev/null 2>&1 || true
[ -z "$old_commit" ] || git reset --hard "$old_commit"
if [ "$had_old" -eq 1 ]; then
    echo "Update failed; restoring previous source, image, and database" >&2
    docker tag nestra:rollback nestra:local
    ./scripts/restore.sh "$backup"
else
    echo "Update failed; database backup: $backup" >&2
fi
exit 1

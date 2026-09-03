#!/usr/bin/env python3
"""Rotate NESTRA_SECRET_KEY and re-encrypt database fields atomically enough for self-hosting.

Stop the service first. Keys come from NESTRA_OLD_SECRET_KEY/NESTRA_NEW_SECRET_KEY or
interactive prompts; they never appear in argv/process listings. If interrupted after the DB
commit, replace .env with the mode-0600 .env.next recovery file before restarting.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import tempfile
from pathlib import Path

from nestra.core.crypto import Crypto


def _key(name: str, prompt: str) -> str:
    return os.environ.get(name) or getpass.getpass(prompt)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _updated_env(path: Path, new_key: str) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith("NESTRA_SECRET_KEY="):
            lines[index] = f"NESTRA_SECRET_KEY={new_key}"
            replaced = True
    if not replaced:
        lines.append(f"NESTRA_SECRET_KEY={new_key}")
    return "\n".join(lines) + "\n"


def rotate(db_path: Path, env_path: Path, old_key: str, new_key: str) -> int:
    old, new = Crypto(old_key), Crypto(new_key)
    env_text = _updated_env(env_path, new_key)
    backup = db_path.with_suffix(db_path.suffix + ".pre-key-rotation")
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    os.chmod(backup, 0o600)

    env_backup = env_path.with_name(env_path.name + ".pre-key-rotation")
    env_backup.write_bytes(env_path.read_bytes())
    os.chmod(env_backup, 0o600)
    next_env = env_path.with_name(env_path.name + ".next")
    fd, temporary = tempfile.mkstemp(prefix=".env.", dir=env_path.parent, text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(env_text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, next_env)
    _fsync_directory(env_path.parent)

    con = sqlite3.connect(db_path)
    changed = 0
    try:
        con.execute("BEGIN IMMEDIATE")
        for table, column in (("notify_targets", "apprise_url_enc"), ("users", "totp_secret")):
            for row_id, blob in con.execute(
                f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"  # noqa: S608
            ):
                plaintext = old.decrypt(bytes(blob))
                con.execute(
                    f"UPDATE {table} SET {column}=? WHERE id=?",  # noqa: S608
                    (new.encrypt(plaintext), row_id),
                )
                changed += 1

        con.commit()
    except BaseException:
        con.rollback()
        next_env.unlink(missing_ok=True)
        raise
    finally:
        con.close()

    # If the process dies after the DB commit, .env.next remains as an explicit recovery file.
    os.replace(next_env, env_path)
    _fsync_directory(env_path.parent)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/db/nestra.db"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    old_key = _key("NESTRA_OLD_SECRET_KEY", "Old key: ")
    new_key = _key("NESTRA_NEW_SECRET_KEY", "New key: ")
    if old_key == new_key:
        parser.error("new key must differ")
    changed = rotate(args.db, args.env_file, old_key, new_key)
    print(f"Rotated {changed} encrypted fields. Restart Nestra now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
from pathlib import Path

import pytest

from nestra.core.crypto import Crypto, DecryptionFailed

ROOT = Path(__file__).parents[2]


def test_shell_scripts_parse_and_are_executable() -> None:
    for name in ("install.sh", "update.sh", "backup.sh", "restore.sh"):
        path = ROOT / "scripts" / name
        assert path.stat().st_mode & 0o111
        subprocess.run(["/bin/sh", "-n", path], check=True)  # noqa: S603
    install = (ROOT / "scripts/install.sh").read_text()
    assert "initial_admin_setup_required" in install and "/setup?token=" in install
    assert "BASE_URL_EXPLICIT" in install and "web.base_url is missing" in install
    assert "Docker Compose is required" in install and "At least 2 GiB" in install
    assert "current_secret" in install and "NESTRA_SECRET_KEY={secret}" in install
    update = (ROOT / "scripts/update.sh").read_text()
    assert "backup=$(./scripts/backup.sh)" in update
    assert 'git reset --hard "$old_commit"' in update
    assert './scripts/restore.sh "$backup"' in update
    backup = (ROOT / "scripts/backup.sh").read_text()
    assert "backup already exists: $out" in backup
    assert "INCLUDE_ATTACHMENTS:-1" in backup and "arcname='models'" in backup
    restore = (ROOT / "scripts/restore.sh").read_text()
    assert "$COMPOSE stop nestra >/dev/null\n" in restore
    assert "refusing to replace a running database" in restore
    assert "replacement_started" in restore and "models.pre-restore" in restore


def test_proxy_examples_only_target_loopback() -> None:
    nginx = (ROOT / "deploy/nginx.example.conf").read_text()
    caddy = (ROOT / "deploy/Caddyfile.example").read_text()
    assert "127.0.0.1:8080" in nginx
    assert "127.0.0.1:8080" in caddy
    assert "proxy_pass http://0.0.0.0" not in nginx


def test_rotate_key_reencrypts_fields_and_env(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("rotate_key", ROOT / "scripts/rotate_key.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    old_key = "old-secret-key-012345678901234567890"
    new_key = "new-secret-key-012345678901234567890"
    old, new = Crypto(old_key), Crypto(new_key)
    db_path = tmp_path / "nestra.db"
    env_path = tmp_path / ".env"
    env_path.write_text(f"NESTRA_SECRET_KEY={old_key}\nOTHER=x\n")
    with sqlite3.connect(db_path) as con:
        con.executescript(
            "CREATE TABLE notify_targets(id INTEGER PRIMARY KEY, apprise_url_enc BLOB);"
            "CREATE TABLE users(id INTEGER PRIMARY KEY, totp_secret BLOB);"
        )
        con.execute("INSERT INTO notify_targets VALUES(1,?)", (old.encrypt("tgram://x"),))
        con.execute("INSERT INTO users VALUES(1,?)", (old.encrypt("TOTPSECRET"),))

    assert module.rotate(db_path, env_path, old_key, new_key) == 2
    with sqlite3.connect(db_path) as con:
        target = con.execute("SELECT apprise_url_enc FROM notify_targets").fetchone()[0]
        totp = con.execute("SELECT totp_secret FROM users").fetchone()[0]
    assert new.decrypt(target) == "tgram://x"
    assert new.decrypt(totp) == "TOTPSECRET"
    with pytest.raises(DecryptionFailed):
        old.decrypt(target)
    assert f"NESTRA_SECRET_KEY={new_key}" in env_path.read_text()
    db_backup = db_path.with_suffix(".db.pre-key-rotation")
    assert db_backup.is_file() and db_backup.stat().st_mode & 0o777 == 0o600
    assert env_path.stat().st_mode & 0o777 == 0o600
    env_backup = env_path.with_name(".env.pre-key-rotation")
    assert env_backup.is_file() and f"NESTRA_SECRET_KEY={old_key}" in env_backup.read_text()
    assert env_backup.stat().st_mode & 0o777 == 0o600

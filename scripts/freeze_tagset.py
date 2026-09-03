#!/usr/bin/env python3
"""Freeze and install a reviewed tagset JSON document."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nestra.core.config import load_settings
from nestra.storage.db import Database
from nestra.tagger.bootstrap.freeze import freeze_tagset

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("document", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--group", required=True)
    args = parser.parse_args()
    settings, _ = load_settings(args.config)
    document = json.loads(args.document.read_text(encoding="utf-8"))
    if document.get("group") != args.group:
        parser.error("document group does not match --group")
    db = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
    try:
        db.migrate()
        frozen = freeze_tagset(document, settings.tagset_path(args.group), db=db)
    finally:
        db.close()
    print(frozen["checksum"])

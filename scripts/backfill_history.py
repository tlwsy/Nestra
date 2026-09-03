#!/usr/bin/env python3
"""Run bounded historical pagination for one DB-backed site without changing its config."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from nestra.core.config import load_settings
from nestra.crawler.service import crawl_site
from nestra.storage.db import Database
from nestra.storage.repositories.sites import get_site, import_yaml_sites

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--pages", type=int)
    args = parser.parse_args()
    settings, _ = load_settings(args.config)
    db = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
    try:
        db.migrate()
        import_yaml_sites(db, settings)
        stored = get_site(db, args.site)
        if stored is None:
            parser.error(f"unknown site: {args.site}")
        pagination = getattr(stored.config.discovery, "pagination", None)
        if pagination is None or pagination.mode == "none":
            parser.error("site discovery has no pagination")
        pages = args.pages or pagination.max_page
        if not pages or not 1 <= pages <= 500:
            parser.error("--pages is required and must be in 1..500")
        pagination.max_pages = pages
        stats = asyncio.run(crawl_site(settings, db, stored))
    finally:
        db.close()
    print(
        f"discovered={stats.discovered} extracted={stats.extracted} "
        f"skipped={stats.skipped} failed={stats.failed}"
    )

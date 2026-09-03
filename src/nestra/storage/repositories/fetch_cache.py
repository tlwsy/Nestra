"""SQLite-backed HTTP conditional request validators."""

from __future__ import annotations

from ...core.time import now_iso
from ...crawler.fetcher import CacheValidators
from ..db import Database


class FetchCacheRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, url: str) -> CacheValidators | None:
        row = self.db.query_one("SELECT etag, last_modified FROM fetch_cache WHERE url=?", (url,))
        return CacheValidators(row["etag"], row["last_modified"]) if row else None

    def put(self, url: str, value: CacheValidators) -> None:
        self.db.execute(
            "INSERT INTO fetch_cache(url, etag, last_modified, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, "
            "last_modified=excluded.last_modified, updated_at=excluded.updated_at",
            (url, value.etag, value.last_modified, now_iso()),
        )

    def delete(self, url: str) -> None:
        self.db.execute("DELETE FROM fetch_cache WHERE url=?", (url,))

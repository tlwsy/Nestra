"""Scheduler registration and per-item isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from nestra.core.config import Settings
from nestra.crawler.attachments import AttachmentStats
from nestra.scheduler import PipelineScheduler
from nestra.scheduler.jobs import (
    JobDependencies,
    crawl_sites,
    download_attachments,
    housekeeping,
)

pytestmark = pytest.mark.unit


class FakeTagger:
    closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeDb:
    def query(self, _sql, _params=()):
        return [
            {"slug": "broken", "consecutive_failures": 0},
            {"slug": "working", "consecutive_failures": 0},
        ]

    def query_one(self, _sql, _params=()):
        return {"consecutive_failures": 1}


def dependencies(**overrides) -> JobDependencies:
    values = {
        "settings": Settings(),
        "db": FakeDb(),
        "tagger": FakeTagger(),
        "matcher": object(),
        "dispatcher": object(),
    }
    values.update(overrides)
    return JobDependencies(**values)


async def test_scheduler_registers_single_instance_jobs() -> None:
    deps = dependencies()
    scheduler = PipelineScheduler(deps)
    scheduler.start()
    try:
        jobs = scheduler._scheduler.get_jobs()
        assert {job.id for job in jobs} == {
            "crawl",
            "attachments",
            "tag",
            "match",
            "delivery",
            "housekeeping",
        }
        assert all(job.max_instances == 1 and job.coalesce for job in jobs)
        assert scheduler.running
    finally:
        await scheduler.aclose()
    assert deps.tagger.closed and not scheduler.running


async def test_crawl_failure_does_not_block_next_site() -> None:
    called: list[str] = []

    def load(_db, slug):
        return SimpleNamespace(config=SimpleNamespace(slug=slug))

    async def crawl(_settings, _db, site):
        called.append(site.config.slug)
        if site.config.slug == "broken":
            raise RuntimeError("boom")

    deps = dependencies(site_loader=load, crawler=crawl)
    assert await crawl_sites(deps) == 1
    assert called == ["broken", "working"]


async def test_housekeeping_keeps_shared_attachment_until_last_reference(
    db, tmp_path: Path
) -> None:
    timestamp = "2025-01-01T00:00:00+00:00"
    group = db.execute(
        "INSERT INTO tagset_groups (slug,name,status,created_at) VALUES ('g','G','draft',?)",
        (timestamp,),
    ).lastrowid
    site = db.execute(
        "INSERT INTO sites (slug,name,base_url,discovery_mode,tagset_group_id,config_json,"
        "created_at,updated_at) VALUES ('s','S','https://example.test','rss',?,'{}',?,?)",
        (group, timestamp, timestamp),
    ).lastrowid
    old = db.execute(
        "INSERT INTO articles (site_id,url,url_hash,status,discovered_at) "
        "VALUES (?,?,?,'NOTIFIED',?)",
        (site, "https://example.test/old", "old", timestamp),
    ).lastrowid
    new = db.execute(
        "INSERT INTO articles (site_id,url,url_hash,status,discovered_at) "
        "VALUES (?,?,?,'NOTIFIED',?)",
        (site, "https://example.test/new", "new", "2026-12-01T00:00:00+00:00"),
    ).lastrowid
    path = tmp_path / "shared"
    path.write_bytes(b"x")
    for article, created, stored_path in (
        (old, timestamp, str(path)),
        (new, "2026-12-01T00:00:00+00:00", "shared"),
    ):
        db.execute(
            "INSERT INTO attachments (article_id,source_url,local_path,status,created_at) "
            "VALUES (?,?,?,'downloaded',?)",
            (article, f"https://example.test/{article}", stored_path, created),
        )
    deps = dependencies(
        db=db,
        settings=Settings(
            storage={"db_path": tmp_path / "db.sqlite", "attachment_dir": tmp_path},
            retention={"attachment_days": 30},
            alerts={"enabled": False},
        ),
    )
    await housekeeping(deps, at=datetime(2026, 2, 1, tzinfo=UTC))
    assert path.is_file()
    assert db.query_one("SELECT COUNT(*) FROM attachments WHERE local_path IS NOT NULL")[0] == 1
    await housekeeping(deps, at=datetime(2027, 2, 1, tzinfo=UTC))
    assert not path.exists()


async def test_housekeeping_keeps_reference_when_file_delete_fails(db, tmp_path: Path) -> None:
    timestamp = "2025-01-01T00:00:00+00:00"
    group = db.execute(
        "INSERT INTO tagset_groups (slug,name,status,created_at) VALUES ('g2','G','draft',?)",
        (timestamp,),
    ).lastrowid
    site = db.execute(
        "INSERT INTO sites (slug,name,base_url,discovery_mode,tagset_group_id,config_json,"
        "created_at,updated_at) VALUES ('s2','S','https://example.test','rss',?,'{}',?,?)",
        (group, timestamp, timestamp),
    ).lastrowid
    article = db.execute(
        "INSERT INTO articles (site_id,url,url_hash,status,discovered_at) "
        "VALUES (?,?,?,'NOTIFIED',?)",
        (site, "https://example.test/a", "a", timestamp),
    ).lastrowid
    path = tmp_path / "kept"
    path.write_bytes(b"x")
    attachment = db.execute(
        "INSERT INTO attachments (article_id,source_url,local_path,status,created_at) "
        "VALUES (?,?,?,'downloaded',?)",
        (article, "https://example.test/file", str(path), timestamp),
    ).lastrowid

    def fail(_path: Path) -> None:
        raise PermissionError("read only")

    deps = dependencies(
        db=db,
        settings=Settings(
            storage={"db_path": tmp_path / "db.sqlite", "attachment_dir": tmp_path},
            retention={"attachment_days": 30},
            alerts={"enabled": False},
        ),
        unlink=fail,
    )
    await housekeeping(deps, at=datetime(2026, 2, 1, tzinfo=UTC))
    assert db.query_one("SELECT local_path FROM attachments WHERE id=?", (attachment,))[0]


async def test_attachment_job_reports_downloaded_and_reused() -> None:
    async def downloader(settings, db, *, limit):
        assert settings and db and limit == 20
        return AttachmentStats(downloaded=2, reused=1)

    deps = dependencies(attachment_downloader=downloader)
    assert await download_attachments(deps) == 3

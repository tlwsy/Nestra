"""Attachment discovery and independent download checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from nestra.core.config import ExtractConfig, Settings, SiteAttachmentConfig
from nestra.crawler.attachments import download_pending
from nestra.crawler.fetcher import BinaryFetchResult, Fetcher
from nestra.extractor.article import extract_article
from nestra.storage.repositories.sites import import_yaml_sites

pytestmark = pytest.mark.unit


def test_extracts_pattern_links_and_enforces_cap() -> None:
    article = extract_article(
        """<html><body><h1>Title</h1><main>Enough body text
        <a href='/system/download.jsp?id=1'>课程表.docx</a>
        <a href='/system/download.jsp?id=2'>第二份.xlsx</a>
        <a href='/ordinary.html'>not attachment</a></main></body></html>""",
        "https://example.test/info/1",
        ExtractConfig(
            min_content_length=1,
            selectors={"title": "h1", "content": "main"},
        ),
        attachment_config=SiteAttachmentConfig(link_patterns=[r"/system/download\.jsp"]),
        max_attachments=1,
    )
    assert len(article.attachments) == 1
    assert article.attachments[0].source_url == "https://example.test/system/download.jsp?id=1"
    assert article.attachments[0].filename == "课程表.docx"


async def test_downloads_and_reuses_content_addressed_file(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        storage={"db_path": tmp_path / "test.db", "attachment_dir": tmp_path / "files"},
        politeness={"respect_robots": False, "delay_sec": 0},
        tagset_groups=[{"slug": "campus", "name": "Campus", "min_docs_for_build": 1}],
        sites=[
            {
                "slug": "site",
                "name": "Site",
                "base_url": "https://example.test",
                "tagset_group": "campus",
                "discovery_mode": "rss",
                "config": {"feed_url": "https://example.test/feed"},
            }
        ],
    )
    import_yaml_sites(db, settings)
    site_id = db.query_one("SELECT id FROM sites WHERE slug='site'")[0]
    article_id = db.execute(
        "INSERT INTO articles (site_id,url,url_hash,status,discovered_at) "
        "VALUES (?,?,?,'EXTRACTED','2026-01-01T00:00:00+00:00')",
        (site_id, "https://example.test/a", "hash"),
    ).lastrowid
    for suffix in ("1", "2"):
        db.execute(
            "INSERT INTO attachments (article_id,source_url,status,created_at) "
            "VALUES (?,?,'pending','2026-01-01T00:00:00+00:00')",
            (article_id, f"https://example.test/download?id={suffix}"),
        )

    async def fetch_bytes(self, url, *, headers=None):
        return BinaryFetchResult(
            url,
            200,
            {
                "content-disposition": (
                    "attachment; filename=plain.pdf; filename*=UTF-8''%E8%AF%BE%E7%A8%8B.pdf"
                )
            },
            b"%PDF-1.7 same file",
        )

    monkeypatch.setattr(Fetcher, "fetch_bytes", fetch_bytes)
    stats = await download_pending(settings, db)
    assert (stats.downloaded, stats.reused, stats.failed) == (1, 1, 0)
    rows = db.query("SELECT * FROM attachments ORDER BY id")
    assert all(row["status"] == "downloaded" for row in rows)
    assert rows[0]["filename"] == "课程.pdf"
    assert rows[0]["sha256"] == rows[1]["sha256"]
    assert rows[0]["local_path"] == rows[1]["local_path"]
    assert Path(rows[0]["local_path"]).is_absolute()
    assert (tmp_path / "files").stat().st_mode & 0o777 == 0o700
    assert Path(rows[0]["local_path"]).read_bytes() == b"%PDF-1.7 same file"

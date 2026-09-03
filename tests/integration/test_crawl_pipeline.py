"""离线 HTML + mock HTTP + SQLite 的 M1 完整流水线。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nestra.core.config import Settings
from nestra.core.errors import SelectorMiss
from nestra.crawler.fetcher import Fetcher
from nestra.crawler.service import crawl_site
from nestra.storage.db import Database
from nestra.storage.repositories.fetch_cache import FetchCacheRepository
from nestra.storage.repositories.sites import get_site, import_yaml_sites

pytestmark = pytest.mark.integration


async def _public(_: str) -> list[str]:
    return ["93.184.216.34"]


def _settings(db_path: Path) -> Settings:
    return Settings(
        storage={"db_path": db_path},
        tagset_groups=[{"slug": "campus", "name": "Campus"}],
        sites=[
            {
                "slug": "ujs",
                "name": "DB site",
                "base_url": "https://jwc.example.com",
                "tagset_group": "campus",
                "discovery_mode": "html_list",
                "config": {
                    "list_urls": ["https://jwc.example.com/index/list.htm"],
                    "item_selector": "li",
                    "url_allow_pattern": r"^https://jwc\.example\.com/(info/|content\.jsp)",
                    "fields": {"url": "a@href", "title": "a@title"},
                },
                "url_canonical": {
                    "rules": [
                        {
                            "match": r"content\.jsp",
                            "extract_params": ["wbtreeid", "wbnewsid"],
                            "rewrite": "/info/{wbtreeid}/{wbnewsid}.htm",
                        }
                    ],
                    "strip_params": ["urltype"],
                },
                "extract": {
                    "min_content_length": 10,
                    "selectors": {"title": "h1", "content": "article"},
                },
                "politeness": {"delay_sec": 0},
            }
        ],
        politeness={
            "delay_sec": 0,
            "retry": {"max_attempts": 1, "backoff_base_sec": 0},
        },
    )


async def test_invalid_discovery_body_does_not_strand_conditional_validator(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "invalid-list.db")
    db = Database(settings.storage.db_path, cache_mb=4)
    db.migrate()
    import_yaml_sites(db, settings)
    stored = get_site(db, "ujs")
    assert stored is not None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<p>incomplete</p>", headers={"ETag": '"bad"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = Fetcher(
            settings.politeness,
            client=client,
            resolver=_public,
            delay_sec=0,
            cache=FetchCacheRepository(db),
        )
        with pytest.raises(SelectorMiss):
            await crawl_site(settings, db, stored, fetcher=fetcher)
    assert db.query_one("SELECT 1 FROM fetch_cache") is None


async def test_pipeline_persists_canonical_articles_and_simhash_dedupe(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "crawl.db")
    db = Database(settings.storage.db_path, cache_mb=4)
    db.migrate()
    import_yaml_sites(db, settings)
    stored = get_site(db, "ujs")
    assert stored is not None

    listing = """
    <ul>
      <li><a title="A" href="/content.jsp?urltype=x&amp;wbtreeid=1&amp;wbnewsid=10">A</a></li>
      <li><a title="B" href="/info/2/20.htm">B</a></li>
    </ul>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/index/list.htm":
            return httpx.Response(200, text=listing, headers={"ETag": '"list-v1"'})
        title = "A" if request.url.path.endswith("/1/10.htm") else "B"
        # Same body deliberately exercises second-line simhash dedupe.
        return httpx.Response(
            200, text=f"<h1>{title}</h1><article>同一篇足够长的正文内容用于近重复检查</article>"
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(
        settings.politeness,
        client=client,
        resolver=_public,
        delay_sec=0,
        cache=FetchCacheRepository(db),
    )
    first = await crawl_site(settings, db, stored, fetcher=fetcher)
    second = await crawl_site(settings, db, stored, fetcher=fetcher)
    await client.aclose()

    assert (first.extracted, first.duplicates, first.failed) == (1, 1, 0)
    assert second.unchanged == 2
    rows = db.query("SELECT url, status, content_html, simhash FROM articles ORDER BY id")
    assert len(rows) == 2
    assert rows[0]["url"] == "https://jwc.example.com/info/1/10.htm"
    assert [row["status"] for row in rows] == ["EXTRACTED", "SKIPPED"]
    assert all("<script" not in row["content_html"] for row in rows)
    assert db.query_one("SELECT etag FROM fetch_cache WHERE url LIKE '%list.htm'")[0] == '"list-v1"'

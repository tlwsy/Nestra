"""Generic discovery dispatch and failed-article retry integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from nestra.core.config import Settings, SiteConfig
from nestra.core.errors import ContentTooShort, FetchFailed
from nestra.core.models import DiscoveredItem, FetchResult
from nestra.crawler.service import crawl_site
from nestra.storage.repositories.sites import StoredSite, import_yaml_sites, sync_yaml_site

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class NoDetailFetcher:
    calls = 0

    async def fetch(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("embedded content must bypass the detail request")

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("mode", "config", "symbol"),
    [
        ("rss", {"feed_url": "https://example.test/feed"}, "discover_rss"),
        ("sitemap", {"sitemap_url": "https://example.test/sitemap.xml"}, "discover_sitemap"),
        (
            "html_list",
            {"list_urls": ["https://example.test/list"], "item_selector": "a"},
            "discover_html_list",
        ),
        (
            "json_api",
            {"endpoint": "https://example.test/api", "field_map": {"url": "url"}},
            "discover_json_api",
        ),
    ],
)
async def test_service_dispatches_all_modes_and_uses_embedded_content(
    mode: str,
    config: dict,
    symbol: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def discover(site, fetcher):
        nonlocal called
        called = True
        return [
            DiscoveredItem(
                "https://example.test/a",
                title="Embedded title",
                content_html="<html><body><h1>Embedded title</h1><p>full body</p></body></html>",
            )
        ]

    monkeypatch.setattr(f"nestra.crawler.service.{symbol}", discover)
    site = SiteConfig(
        slug="site",
        name="Site",
        base_url="https://example.test",
        tagset_group="group",
        discovery_mode=mode,
        config=config,
        extract={"min_content_length": 1},
    )
    stats = await crawl_site(
        Settings(), None, StoredSite(0, site), dry_run=True, fetcher=NoDetailFetcher()
    )
    assert called and stats.extracted == 1


async def test_malformed_discovered_url_does_not_hide_valid_articles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def discover(_site, _fetcher):
        body = "<html><body><h1>Title</h1><p>body content</p></body></html>"
        return [
            DiscoveredItem("https://example.test:not-a-port/bad", title="Title", content_html=body),
            DiscoveredItem("https://example.test/good", title="Title", content_html=body),
        ]

    monkeypatch.setattr("nestra.crawler.service.discover_rss", discover)
    site = SiteConfig(
        slug="site",
        name="Site",
        base_url="https://example.test",
        tagset_group="group",
        discovery_mode="rss",
        config={"feed_url": "https://example.test/feed"},
        extract={"min_content_length": 1},
    )
    stats = await crawl_site(
        Settings(), None, StoredSite(0, site), dry_run=True, fetcher=NoDetailFetcher()
    )
    assert stats.failed == 1 and stats.extracted == 1


async def test_failed_article_retries_when_discovery_is_unchanged(
    db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        storage={"db_path": tmp_path / "test.db"},
        politeness={
            "respect_robots": False,
            "delay_sec": 0,
            "retry": {"max_attempts": 3, "backoff_base_sec": 0},
        },
        tagset_groups=[{"slug": "group", "name": "Group", "min_docs_for_build": 1}],
        sites=[
            {
                "slug": "site",
                "name": "Site",
                "base_url": "https://example.test",
                "tagset_group": "group",
                "discovery_mode": "rss",
                "config": {"feed_url": "https://example.test/feed"},
                "extract": {
                    "min_content_length": 1,
                    "selectors": {"title": "h1", "content": "main"},
                },
            }
        ],
    )
    import_yaml_sites(db, settings)
    stored = StoredSite(
        db.query_one("SELECT id FROM sites WHERE slug='site'")[0], settings.sites[0]
    )
    rounds = 0

    async def discover(_site, _fetcher):
        nonlocal rounds
        rounds += 1
        return [DiscoveredItem("https://example.test/a")] if rounds == 1 else []

    class DetailFetcher:
        calls = 0

        async def fetch(self, url, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise FetchFailed("temporary")
            html = "<h1>Recovered</h1><main>body recovered</main>"
            return FetchResult(url, url, 200, html, "utf-8")

        async def close(self):
            return None

    monkeypatch.setattr("nestra.crawler.service.discover_rss", discover)
    fetcher = DetailFetcher()
    assert (await crawl_site(settings, db, stored, fetcher=fetcher)).failed == 1
    article = db.query_one("SELECT status,last_error FROM articles")
    assert article["status"] == "FAILED" and "temporary" in article["last_error"]
    site = db.query_one("SELECT consecutive_failures,last_error FROM sites")
    assert site["consecutive_failures"] == 0 and site["last_error"] is None
    assert db.query_one("SELECT attempts FROM articles")[0] == 1
    assert (await crawl_site(settings, db, stored, fetcher=fetcher)).extracted == 1
    assert db.query_one("SELECT status FROM articles")[0] == "EXTRACTED"
    assert db.query_one("SELECT consecutive_failures FROM sites")[0] == 0


async def test_fatal_article_waits_for_explicit_site_config_sync(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        storage={"db_path": tmp_path / "test.db"},
        politeness={"respect_robots": False, "delay_sec": 0},
        tagset_groups=[{"slug": "group", "name": "Group", "min_docs_for_build": 1}],
        sites=[
            {
                "slug": "site",
                "name": "Site",
                "base_url": "https://example.test",
                "tagset_group": "group",
                "discovery_mode": "rss",
                "config": {"feed_url": "https://example.test/feed"},
                "extract": {
                    "min_content_length": 1,
                    "selectors": {"title": "h1", "content": "main"},
                },
            }
        ],
    )
    import_yaml_sites(db, settings)
    stored = StoredSite(
        db.query_one("SELECT id FROM sites WHERE slug='site'")[0], settings.sites[0]
    )
    rounds = 0

    async def discover(_site, _fetcher):
        nonlocal rounds
        rounds += 1
        return [DiscoveredItem("https://example.test/a")] if rounds == 1 else []

    class DetailFetcher:
        calls = 0

        async def fetch(self, url, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ContentTooShort("bad selector")
            return FetchResult(
                url,
                url,
                200,
                "<h1>Recovered</h1><main>body recovered</main>",
                "utf-8",
            )

        async def close(self):
            return None

    monkeypatch.setattr("nestra.crawler.service.discover_rss", discover)
    fetcher = DetailFetcher()
    assert (await crawl_site(settings, db, stored, fetcher=fetcher)).failed == 1
    assert db.query_one("SELECT next_attempt_at FROM articles")[0] is None
    assert (await crawl_site(settings, db, stored, fetcher=fetcher)).extracted == 0
    assert fetcher.calls == 1

    sync_yaml_site(db, settings, "site")
    assert db.query_one("SELECT status FROM articles")[0] == "DISCOVERED"
    assert (await crawl_site(settings, db, stored, fetcher=fetcher)).extracted == 1

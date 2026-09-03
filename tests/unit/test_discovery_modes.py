"""Offline checks for the three generic discovery modes added after M1."""

from __future__ import annotations

from nestra.core.config import SiteConfig
from nestra.core.models import FetchResult
from nestra.crawler.discovery.json_api import discover as discover_json
from nestra.crawler.discovery.rss import discover as discover_rss
from nestra.crawler.discovery.sitemap import discover as discover_sitemap


class FakeFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    async def fetch(self, url: str, **_kwargs) -> FetchResult:
        self.urls.append(url)
        return FetchResult(url, url, 200, self.pages[url], "utf-8")


async def test_rss_reads_full_content() -> None:
    url = "https://example.test/feed.xml"
    body = "x" * 600
    feed = f"""<rss version="2.0"><channel><title>Feed</title><item>
    <title>One</title><link>https://example.test/one</link>
    <description>Summary</description><content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">{body}</content:encoded>
    </item></channel></rss>"""
    site = SiteConfig(
        slug="rss",
        name="RSS",
        base_url="https://example.test",
        tagset_group="g",
        discovery_mode="rss",
        config={"feed_url": url, "content_from_feed": "auto"},
    )
    items = await discover_rss(site, FakeFetcher({url: feed}))
    assert items[0].url.endswith("/one") and items[0].content_html == body


async def test_nested_sitemap_and_documented_url_pattern() -> None:
    index = "https://example.test/sitemap.xml"
    child = "https://example.test/posts.xml"
    pages = {
        index: (
            "<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
            f"<sitemap><loc>{child}</loc></sitemap></sitemapindex>"
        ),
        child: (
            "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
            "<url><loc>https://example.test/posts/1</loc><lastmod>2026-01-02</lastmod></url>"
            "<url><loc>https://example.test/private/2</loc></url></urlset>"
        ),
    }
    site = SiteConfig(
        slug="map",
        name="Map",
        base_url="https://example.test",
        tagset_group="g",
        discovery_mode="sitemap",
        config={"sitemap_url": index, "url_pattern": r"/posts/"},
    )
    items = await discover_sitemap(site, FakeFetcher(pages))
    assert [item.url for item in items] == ["https://example.test/posts/1"]


async def test_json_endpoint_page_placeholder_infers_pagination() -> None:
    first = "https://example.test/api?page=1"
    second = "https://example.test/api?page=2"
    fetcher = FakeFetcher(
        {
            first: '{"data":[{"url":"/one","title":"One"}]}',
            second: '{"data":[{"url":"/two","title":"Two"}]}',
        }
    )
    site = SiteConfig(
        slug="api",
        name="API",
        base_url="https://example.test",
        tagset_group="g",
        discovery_mode="json_api",
        config={
            "endpoint": "https://example.test/api?page={page}",
            "field_map": {"url": "url", "title": "title"},
            "max_pages": 2,
        },
    )
    items = await discover_json(site, fetcher)
    assert [item.title for item in items] == ["One", "Two"]
    assert fetcher.urls == [first, second]

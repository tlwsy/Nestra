"""M1 URL、发现、提取与 HTTP 边界测试（全离线）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from nestra.core.config import (
    ExtractConfig,
    HtmlListDiscovery,
    PolitenessConfig,
    SiteConfig,
    UrlCanonicalConfig,
)
from nestra.core.errors import ResponseTooLarge, RobotsDenied, SsrfBlocked
from nestra.core.models import FetchResult
from nestra.crawler.discovery.html_list import discover, parse_list
from nestra.crawler.fetcher import Fetcher
from nestra.crawler.url_canonical import canonicalize_url
from nestra.extractor.article import extract_article
from nestra.extractor.dedupe import hamming_distance, simhash
from nestra.extractor.sanitize import sanitize_html

pytestmark = pytest.mark.unit


async def _public(_: str) -> list[str]:
    return ["93.184.216.34"]


def _politeness(**kwargs) -> PolitenessConfig:
    return PolitenessConfig(
        delay_sec=0,
        timeout_sec=1,
        retry={"max_attempts": 3, "backoff_base_sec": 0},
        **kwargs,
    )


def test_ujs_canonicalization_and_generic_normalization() -> None:
    config = UrlCanonicalConfig(
        rules=[
            {
                "match": r"content\.jsp",
                "extract_params": ["wbtreeid", "wbnewsid"],
                "rewrite": "/info/{wbtreeid}/{wbnewsid}.htm",
            }
        ],
        strip_params=["urltype", "utm_source"],
    )
    dynamic = "https://JWC.UJS.EDU.CN/content.jsp?urltype=x&wbnewsid=30031&wbtreeid=1331#top"
    assert canonicalize_url(dynamic, "https://jwc.ujs.edu.cn", config) == (
        "https://jwc.ujs.edu.cn/info/1331/30031.htm"
    )
    assert canonicalize_url("/a/?z=2&a=1#x", "https://EXAMPLE.com", config) == (
        "https://example.com/a?a=1&z=2"
    )
    assert canonicalize_url("https://[2606:4700:4700::1111]:443/a", "https://x", config) == (
        "https://[2606:4700:4700::1111]/a"
    )
    assert canonicalize_url("https://例子.测试/a", "https://x", config) == (
        "https://xn--fsqu00a.xn--0zwm56d/a"
    )


@pytest.mark.parametrize("url", ["https://example.test/line\nbreak", "x" * 4097])
def test_canonicalization_rejects_unsafe_url_text(url: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_url(url, "https://example.test", UrlCanonicalConfig())


def test_html_list_uses_title_attribute_and_filters_external(fixtures_dir: Path) -> None:
    config = HtmlListDiscovery(
        list_urls=["https://jwc.ujs.edu.cn/index/tzgg.htm"],
        item_selector='li[id^="line_"]',
        url_allow_pattern=r"^https://jwc\.ujs\.edu\.cn/(info/|content\.jsp)",
        fields={"url": "a.title.tt1@href", "title": "a.title.tt1@title", "published_at": "p.date"},
    )
    items = parse_list(
        (fixtures_dir / "ujs_list.html").read_text(encoding="utf-8"),
        config.list_urls[0],
        config,
    )
    assert [item.title for item in items] == ["完整的选课停开通知", "开放实验项目申报"]
    assert items[0].url.endswith(
        "content.jsp?urltype=news.NewsContentUrl&wbtreeid=1331&wbnewsid=30031"
    )
    assert items[0].published_at == datetime(2026, 7, 21, tzinfo=UTC)


async def test_desc_index_pagination_starts_at_max_page_minus_one() -> None:
    site = SiteConfig(
        slug="s",
        name="S",
        base_url="https://example.com",
        tagset_group="g",
        discovery_mode="html_list",
        config={
            "list_urls": ["https://example.com/index.htm"],
            "item_selector": "a",
            "pagination": {
                "mode": "url_template",
                "template": "https://example.com/index/{page}.htm",
                "order": "desc_index",
                "max_page": 5,
                "max_pages": 3,
            },
        },
    )

    class FakeFetcher:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def fetch(self, url: str) -> FetchResult:
            self.urls.append(url)
            return FetchResult(url, url, 200, '<a href="/article">A</a>', "utf-8")

    fetcher = FakeFetcher()
    await discover(site, fetcher)  # type: ignore[arg-type]
    assert fetcher.urls == [
        "https://example.com/index.htm",
        "https://example.com/index/4.htm",
        "https://example.com/index/3.htm",
    ]


async def test_template_pagination_advances_past_unchanged_entry_page() -> None:
    site = SiteConfig(
        slug="s",
        name="S",
        base_url="https://example.com",
        tagset_group="g",
        discovery_mode="html_list",
        config={
            "list_urls": ["https://example.com/index.htm"],
            "item_selector": "a",
            "pagination": {
                "mode": "url_template",
                "template": "https://example.com/index/{page}.htm",
                "order": "desc_index",
                "max_page": 5,
                "max_pages": 2,
            },
        },
    )

    class FakeFetcher:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def fetch(self, url: str) -> FetchResult:
            self.urls.append(url)
            if len(self.urls) == 1:
                return FetchResult(url, url, 304, "", "", from_cache=True)
            return FetchResult(url, url, 200, '<a href="/old">Old</a>', "utf-8")

    fetcher = FakeFetcher()
    items = await discover(site, fetcher)  # type: ignore[arg-type]
    assert [item.url for item in items] == ["https://example.com/old"]
    assert fetcher.urls[-1].endswith("/4.htm")


def test_extract_sanitizes_and_strips_site_elements(fixtures_dir: Path) -> None:
    article = extract_article(
        (fixtures_dir / "ujs_article.html").read_text(encoding="utf-8"),
        "https://jwc.ujs.edu.cn/info/1331/30031.htm",
        ExtractConfig(
            min_content_length=20,
            selectors={
                "title": "h1.title",
                "content": "div.v_news_content",
                "published_at_regex": r"发布时间：\s*([\d-]+)",
            },
            strip_selectors=[".related"],
        ),
    )
    assert article.title == "完整的选课停开通知"
    assert article.published_at == datetime(2026, 7, 21, tzinfo=UTC)
    assert "相关推荐" not in article.content_text
    assert "alert" not in article.content_text
    assert "script" not in article.content_html
    assert "onerror" not in article.content_html
    assert 'href="https://jwc.ujs.edu.cn/notice/details"' in article.content_html


def test_published_at_regex_without_capture_uses_full_match(fixtures_dir: Path) -> None:
    article = extract_article(
        (fixtures_dir / "ujs_article.html").read_text(encoding="utf-8"),
        "https://jwc.ujs.edu.cn/info/1331/30031.htm",
        ExtractConfig(
            min_content_length=20,
            selectors={
                "title": "h1.title",
                "content": "div.v_news_content",
                "published_at_regex": r"\d{4}-\d{2}-\d{2}",
            },
        ),
    )
    assert article.published_at == datetime(2026, 7, 21, tzinfo=UTC)


def test_sanitize_blocks_dangerous_links() -> None:
    cleaned = sanitize_html('<a href="javascript:alert(1)" onclick="x">bad</a>')
    assert "javascript" not in cleaned
    assert "onclick" not in cleaned


def test_simhash_is_stable_and_detects_small_change() -> None:
    body = "选课通知 请各学院及时安排学生完成选课"
    assert simhash(body) == simhash(body)
    assert hamming_distance(simhash(body), simhash(body + "。")) <= 3
    assert hamming_distance("0000000000000000", "ffffffffffffffff") == 64


async def test_fetcher_retries_and_uses_persistent_validators() -> None:
    calls = 0
    article_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        article_headers.append(request.headers)
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        if calls == 2:
            return httpx.Response(200, text="ok", headers={"ETag": '"v1"'})
        return httpx.Response(304)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with Fetcher(_politeness(), client=client, resolver=_public) as fetcher:
        first = await fetcher.fetch("https://example.com/article")
        second = await fetcher.fetch("https://example.com/article")
    await client.aclose()

    assert first.html == "ok"
    assert second.from_cache and second.status_code == 304
    assert article_headers[-1]["if-none-match"] == '"v1"'
    assert calls == 3


async def test_fetcher_uses_html_meta_charset_without_http_charset() -> None:
    body = '<meta charset="gb2312"><main>教务通知</main>'.encode("gb18030")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"Content-Type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(_politeness(respect_robots=False), client=client, resolver=_public)
    result = await fetcher.fetch("https://example.com/article")
    await client.aclose()
    assert result.encoding == "gb18030"
    assert "教务通知" in result.html


async def test_fetcher_honors_retry_after_for_429() -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls += 1
        return (
            httpx.Response(429, headers={"Retry-After": "999999"})
            if calls == 1
            else httpx.Response(200, text="ok")
        )

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(_politeness(), client=client, resolver=_public, sleep=sleep)
    assert (await fetcher.fetch("https://example.com/article")).html == "ok"
    assert waits == [300.0]
    await client.aclose()


async def test_owned_fetcher_connects_to_validated_ip() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(_politeness(respect_robots=False), resolver=_public)
    await fetcher._client.aclose()
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await fetcher.fetch("https://example.com/article")
    finally:
        await fetcher.close()
    assert result.final_url == "https://example.com/article"
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "example.com"


async def test_fetcher_drops_credentials_on_cross_origin_redirect() -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": "https://other.example/file"})
        seen.append(request.headers)
        return httpx.Response(200, content=b"ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(_politeness(respect_robots=False), client=client, resolver=_public)
    await fetcher.fetch_bytes(
        "https://example.com/file",
        headers={
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "Referer": "https://example.com/private?token=secret",
        },
    )
    await client.aclose()
    assert not {"authorization", "cookie", "referer"}.intersection(seen[0])


async def test_fetcher_blocks_private_targets_before_http() -> None:
    def unreachable(_: httpx.Request) -> httpx.Response:
        raise AssertionError("private target must not reach HTTP transport")

    client = httpx.AsyncClient(transport=httpx.MockTransport(unreachable))
    fetcher = Fetcher(_politeness(), client=client)
    with pytest.raises(SsrfBlocked):
        await fetcher.fetch("http://127.0.0.1/private")
    await client.aclose()


async def test_fetcher_respects_robots_and_response_limit() -> None:
    def denied(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        raise AssertionError("denied URL must not be requested")

    client = httpx.AsyncClient(transport=httpx.MockTransport(denied))
    fetcher = Fetcher(_politeness(), client=client, resolver=_public)
    with pytest.raises(RobotsDenied):
        await fetcher.fetch("https://example.com/private/a")
    await client.aclose()

    def large(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"12345", headers={"Content-Length": "5"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(large))
    fetcher = Fetcher(_politeness(), client=client, resolver=_public, max_bytes=4)
    with pytest.raises(ResponseTooLarge):
        await fetcher.fetch("https://example.com/article")
    await client.aclose()

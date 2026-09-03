"""M7 onboarding detection and preview tests; no real network."""

from __future__ import annotations

import httpx
import pytest

from nestra.core.config import SiteConfig
from nestra.core.models import DiscoveredItem, FetchResult
from nestra.onboarding.analysis import analyze_html, infer_pagination_direction
from nestra.onboarding.detect.attachment import detect_attachment_patterns
from nestra.onboarding.detect.dualform import confirm_dual_form, dual_form_pairs
from nestra.onboarding.dryrun import DryRunLimits, preview_site
from nestra.onboarding.probe import ProbeLimitExceeded, ProbeLimits, SafeFetcher, probe_site

pytestmark = pytest.mark.unit
PUBLIC_V4 = "93.184.216.34"


def public_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return (PUBLIC_V4,)


def test_probe_validates_feed_sitemap_and_discovers_list_candidates() -> None:
    homepage = """
        <html><head><title>Example</title>
        <link rel="alternate" type="application/atom+xml" href="/atom.xml">
        </head><body><a href="/notices">Notices and news</a></body></html>
    """
    rows = "".join(
        f'<li class="notice"><a class="title" href="/article/{number}">Announcement {number}</a>'
        f'<time class="date">2025-01-{number:02d}</time></li>'
        for number in range(1, 6)
    )
    list_html = f"<html><head><title>News</title></head><body><ul>{rows}</ul></body></html>"
    atom = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>'
    sitemap = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" />'

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/":
            return httpx.Response(200, text=homepage)
        if path == "/atom.xml":
            return httpx.Response(200, text=atom)
        if path == "/robots.txt":
            return httpx.Response(200, text="Sitemap: https://site.example/map.xml")
        if path == "/map.xml":
            return httpx.Response(200, text=sitemap)
        if path == "/notices":
            return httpx.Response(200, text=list_html)
        if path.startswith("/article/"):
            return httpx.Response(
                200,
                text=(
                    "<html><head><title>Announcement</title></head><body><article>"
                    + "article body " * 20
                    + '<a href="/download.jsp?id=7">form</a></article></body></html>'
                ),
            )
        return httpx.Response(404)

    report = probe_site(
        "https://site.example/",
        limits=ProbeLimits(max_pages=15, sample_articles=2),
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    )
    assert report.get("feed").value == "https://site.example/atom.xml"  # type: ignore[union-attr]
    assert report.get("sitemap").value == "https://site.example/map.xml"  # type: ignore[union-attr]
    lists = report.get("list_pages")
    assert lists is not None and lists.value == "https://site.example/notices"
    selector = lists.candidates[0]["selector_candidates"][0]
    assert selector["published_at_selector"] == "time.date"
    attachments = report.get("attachments.link_patterns")
    assert attachments is not None and r"download\.jsp" in attachments.value
    candidate = report.get("config_candidate")
    assert candidate is not None
    assert r"download\.jsp" in candidate.value["attachments"]["link_patterns"]


def test_pagination_direction_requires_cross_page_date_evidence() -> None:
    rows = "".join(
        f'<li class="row"><a href="/article/{i}">Long article title {i}</a>'
        f"<time>2025-02-{i:02d}</time></li>"
        for i in range(1, 6)
    )
    entry = (
        f"<html><head><title>News archive</title></head><body><ul>{rows}</ul>"
        '<a href="/archive/1.htm">1</a><a href="/archive/2.htm">2</a></body></html>'
    )
    older = entry.replace("2025-02", "2020-02")
    candidate = next(
        item
        for item in analyze_html(entry, "https://site.example/archive/").pagination_candidates
        if item.config["mode"] == "url_template"
    )
    inferred = infer_pagination_direction(entry, older, candidate)
    assert inferred.direction == "newer_to_older"
    assert inferred.config == {
        "mode": "url_template",
        "template": "https://site.example/archive/{page}.htm",
        "max_page": 2,
        "order": "desc_index",
    }


def test_dual_forms_need_matching_content_and_attachment_patterns_are_bounded() -> None:
    query = "https://site.example/content.jsp?urltype=news.NewsContentUrl&wbtreeid=12&wbnewsid=34"
    path = "https://site.example/info/12/34.htm"
    assert dual_form_pairs((query, path)) == ((query, path),)
    html = (
        "<html><head><title>Same article</title></head><body><article>"
        + "substantial matching article text " * 8
        + '<a href="/files/form.pdf">PDF</a><a href="/download.jsp?id=8">DOC</a>'
        + "</article></body></html>"
    )
    confirmed = confirm_dual_form(query, html, path, html)
    assert confirmed is not None and confirmed.parameter_mapping == (
        ("wbtreeid", "12"),
        ("wbnewsid", "34"),
    )
    patterns = detect_attachment_patterns([(path, html)])
    assert {item.pattern for item in patterns} == {r"\.pdf(?:$|[?#])", r"download\.jsp"}


def test_safe_fetcher_page_budget_counts_redirect_hops_globally() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": f"/hop/{calls}"})

    fetcher = SafeFetcher(
        limits=ProbeLimits(max_pages=2),
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProbeLimitExceeded, match="2 page requests"):
        fetcher.fetch("https://site.example/")
    assert calls == 2


async def test_preview_site_extracts_each_item_and_keeps_errors_without_persistence() -> None:
    site = SiteConfig.model_validate(
        {
            "slug": "example",
            "name": "Example",
            "base_url": "https://site.example/",
            "tagset_group": "news",
            "discovery_mode": "html_list",
            "config": {"list_urls": ["https://site.example/list"], "item_selector": "li"},
            "extract": {"min_content_length": 100},
        }
    )

    async def discoverer(_site, _fetcher):
        return [
            DiscoveredItem("https://site.example/good", title="Good title"),
            DiscoveredItem("https://site.example/bad", title="Bad title"),
        ]

    class FakeFetcher:
        async def fetch(self, url: str, *, use_conditional: bool = True) -> FetchResult:
            body = "useful article body " * 20 if url.endswith("/good") else "short"
            html = f"<html><body><article>{body}</article></body></html>"
            return FetchResult(url, url, 200, html, "utf-8")

    report = await preview_site(
        site,
        fetcher=FakeFetcher(),
        discoverer=discoverer,
        limits=DryRunLimits(sample_size=2, max_pages=2),
    )
    assert (report.discovered, report.succeeded, report.failed) == (2, 1, 1)
    assert report.items[0].title == "Good title"
    assert report.items[0].content_length >= 100
    assert report.items[1].success is False
    assert report.items[1].error.startswith("ContentTooShort:")

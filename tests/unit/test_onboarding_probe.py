"""Standalone onboarding probe and SSRF boundary tests (no real network)."""

from __future__ import annotations

import httpx
import pytest

from nestra.core.errors import SsrfBlocked
from nestra.onboarding.emit import emit_config
from nestra.onboarding.probe import (
    ProbeLimitExceeded,
    ProbeLimits,
    ProbeTimedOut,
    SafeFetcher,
    probe_site,
)
from nestra.onboarding.ssrf import resolve_url

pytestmark = pytest.mark.unit
PUBLIC_V4 = "93.184.216.34"


def public_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return (PUBLIC_V4,)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/",
        "http://10.2.3.4/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[ff02::1]/",
        "http://[::ffff:127.0.0.1]/",
    ],
)
def test_private_and_special_ip_literals_are_blocked(url: str) -> None:
    with pytest.raises(SsrfBlocked, match="not public"):
        resolve_url(url)


def test_public_ipv6_literal_is_pinned_without_dns() -> None:
    target = resolve_url("https://[2606:4700:4700::1111]/path")
    assert target.pinned_url == "https://[2606:4700:4700::1111]/path"
    assert target.host_header == "[2606:4700:4700::1111]"


def test_every_dns_answer_must_be_public() -> None:
    def malicious_dns(_host: str, _port: int) -> tuple[str, ...]:
        return (PUBLIC_V4, "127.0.0.1")

    with pytest.raises(SsrfBlocked, match=r"127\.0\.0\.1"):
        resolve_url("https://attacker.example/", malicious_dns)


def test_credentials_and_non_http_schemes_are_blocked() -> None:
    with pytest.raises(SsrfBlocked, match="credentials"):
        resolve_url("https://user:secret@example.com/", public_resolver)
    with pytest.raises(SsrfBlocked, match="only http"):
        resolve_url("file:///etc/passwd", public_resolver)


def test_dns_is_pinned_while_host_header_is_preserved() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<html><body>public</body></html>")

    page = SafeFetcher(
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    ).fetch("https://news.example/path")
    assert page.status_code == 200
    assert seen[0].url.host == PUBLIC_V4
    assert seen[0].headers["host"] == "news.example"
    assert seen[0].extensions["sni_hostname"] == "news.example"


def test_redirect_target_is_validated_before_second_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    fetcher = SafeFetcher(
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SsrfBlocked, match=r"127\.0\.0\.1"):
        fetcher.fetch("https://public.example/")
    assert calls == 1


def test_advertised_and_streamed_size_limits() -> None:
    advertised = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-length": "11"},
            content=b"tiny",
        )
    )
    with pytest.raises(ProbeLimitExceeded, match="advertises"):
        SafeFetcher(
            limits=ProbeLimits(max_bytes_per_page=10),
            resolver=public_resolver,
            transport=advertised,
        ).fetch("https://public.example/")

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"123456"
            yield b"789012"

    streamed = httpx.MockTransport(lambda _request: httpx.Response(200, stream=Chunks()))
    with pytest.raises(ProbeLimitExceeded, match="exceeded"):
        SafeFetcher(
            limits=ProbeLimits(max_bytes_per_page=10),
            resolver=public_resolver,
            transport=streamed,
        ).fetch("https://public.example/")


def test_transport_timeout_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProbeTimedOut, match="timed out"):
        SafeFetcher(
            limits=ProbeLimits(timeout_sec=0.01),
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
        ).fetch("https://public.example/")


def test_public_static_page_produces_ranked_dry_run_config() -> None:
    items = "".join(
        f'<li class="news-item"><a class="title" href="/news/{i}" '
        f'title="Complete announcement title {i}">Short {i}...</a>'
        f"<time>2025-01-{i:02d}</time></li>"
        for i in range(1, 7)
    )
    html = f"""
        <html><head><title>Example News</title></head><body>
        <ul>{items}</ul>
        <nav><a href="?page=1">1</a><a href="?page=2">2</a>
        <a class="next" rel="next" href="?page=2">Next</a></nav>
        </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == PUBLIC_V4
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=html)

    report = probe_site(
        "https://news.example/list",
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    )
    selectors = report.get("item_selector")
    pagination = report.get("pagination")
    assert selectors is not None and selectors.value == "li.news-item"
    assert selectors.candidates[0]["title_selector"] == "a.title@title"
    assert tuple(c["confidence"] for c in selectors.candidates) == tuple(
        sorted((c["confidence"] for c in selectors.candidates), reverse=True)
    )
    assert pagination is not None and pagination.value["mode"] == "next_link"

    config = emit_config(report, tagset_group="news")
    assert config["enabled"] is False
    assert config["tagset_group"] == "news"
    assert config["config"]["fields"] == {
        "url": "a.title@href",
        "title": "a.title@title",
        "published_at": "time",
    }

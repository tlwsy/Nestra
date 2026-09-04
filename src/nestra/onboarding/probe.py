"""Bounded, SSRF-safe standalone onboarding probe."""

from __future__ import annotations

import ipaddress
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from selectolax.parser import HTMLParser

from nestra.core.errors import CrawlError, SsrfBlocked
from nestra.core.models import ProbeFinding, ProbeReport
from nestra.crawler.fetcher import detect_encoding

from .analysis import HtmlAnalysis, analyze_html, infer_pagination_direction
from .detect.attachment import detect_attachment_patterns
from .detect.dualform import confirm_dual_form, dual_form_pairs
from .detect.feed import (
    feed_candidates,
    identify_feed,
    identify_sitemap,
    sitemap_candidates,
)
from .detect.listpage import navigation_candidates
from .ssrf import ResolvedUrl, Resolver, resolve_url, system_resolver


class ProbeError(CrawlError):
    """A probe failed without writing or persisting anything."""


class ProbeLimitExceeded(ProbeError):
    pass


class ProbeTimedOut(ProbeError):
    pass


@dataclass(frozen=True, slots=True)
class ProbeLimits:
    max_pages: int = 40
    max_bytes_per_page: int = 3_145_728
    timeout_sec: float = 20
    max_duration_sec: float = 120
    max_redirects: int = 10
    sample_articles: int = 6
    delay_sec: float = 0


@dataclass(frozen=True, slots=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status_code: int
    content: bytes
    encoding: str
    redirects: tuple[str, ...]

    @property
    def text(self) -> str:
        try:
            return self.content.decode(self.encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


def _peer_address(response: httpx.Response) -> str | None:
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        return None
    peer = stream.get_extra_info("server_addr")
    if isinstance(peer, (tuple, list)) and peer:
        return str(peer[0])
    return str(peer) if peer else None


def _same_ip(actual: str, expected: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    try:
        address = ipaddress.ip_address(actual.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if isinstance(expected, ipaddress.IPv6Address) and expected.ipv4_mapped:
        expected = expected.ipv4_mapped
    return address == expected


class SafeFetcher:
    """Fetch one page with DNS pinning, manual redirects, and hard byte/time limits."""

    def __init__(
        self,
        *,
        limits: ProbeLimits | None = None,
        resolver: Resolver = system_resolver,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limits = limits or ProbeLimits()
        self.resolver = resolver
        self.transport = transport
        self.clock = clock
        self.sleeper = sleeper
        self._started = clock()
        self._requests = 0
        self._last_started = 0.0

    @property
    def pages_fetched(self) -> int:
        return self._requests

    def fetch(self, url: str) -> FetchedPage:
        current = url
        redirects: list[str] = []
        deadline = self._started + self.limits.max_duration_sec
        with httpx.Client(
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "Nestra onboarding probe/1.0", "Accept": "text/html,*/*;q=0.1"},
        ) as client:
            for hop in range(self.limits.max_redirects + 1):
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise ProbeTimedOut(f"probe exceeded {self.limits.max_duration_sec}s")
                if self._requests >= self.limits.max_pages:
                    raise ProbeLimitExceeded(
                        f"probe exceeded {self.limits.max_pages} page requests"
                    )
                target = resolve_url(current, self.resolver)
                wait = self._last_started + self.limits.delay_sec - self.clock()
                if wait > 0:
                    self.sleeper(min(wait, remaining))
                self._last_started = self.clock()
                remaining = deadline - self._last_started
                if remaining <= 0:
                    raise ProbeTimedOut(f"probe exceeded {self.limits.max_duration_sec}s")
                self._requests += 1
                try:
                    page = self._request(
                        client,
                        target,
                        min(self.limits.timeout_sec, remaining),
                        deadline,
                    )
                except httpx.TimeoutException as exc:
                    raise ProbeTimedOut(f"request timed out: {current}") from exc
                location = page.headers.get("location")
                if page.is_redirect and location:
                    if hop == self.limits.max_redirects:
                        raise ProbeLimitExceeded(f"more than {self.limits.max_redirects} redirects")
                    current = urljoin(current, location)
                    redirects.append(current)
                    continue
                if page.status_code >= 400:
                    raise ProbeError(f"HTTP {page.status_code} for {current}")
                encoding = detect_encoding(page.headers, page.content)
                return FetchedPage(
                    url,
                    current,
                    page.status_code,
                    page.content,
                    encoding,
                    tuple(redirects),
                )
        raise ProbeLimitExceeded("redirect limit exceeded")  # pragma: no cover

    def _request(
        self,
        client: httpx.Client,
        target: ResolvedUrl,
        timeout: float,
        deadline: float,
    ) -> httpx.Response:
        headers = {"Host": target.host_header}
        extensions = {"sni_hostname": target.hostname}
        with client.stream(
            "GET",
            target.pinned_url,
            headers=headers,
            extensions=extensions,
            timeout=httpx.Timeout(timeout),
        ) as response:
            peer = _peer_address(response)
            if peer is not None and not _same_ip(peer, target.ip):
                raise SsrfBlocked(
                    target.url,
                    f"connected peer {peer} differs from pinned {target.ip}",
                )
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    advertised = int(content_length)
                except ValueError:
                    advertised = 0
                if advertised > self.limits.max_bytes_per_page:
                    raise ProbeLimitExceeded(
                        f"page advertises {advertised} bytes; "
                        f"limit is {self.limits.max_bytes_per_page}"
                    )
            content = bytearray()
            for chunk in response.iter_bytes():
                if self.clock() > deadline:
                    raise ProbeTimedOut(f"probe exceeded {self.limits.max_duration_sec}s")
                content.extend(chunk)
                if len(content) > self.limits.max_bytes_per_page:
                    raise ProbeLimitExceeded(
                        f"page exceeded {self.limits.max_bytes_per_page} bytes"
                    )
            # iter_bytes() already decoded gzip/br content. Do not make httpx decode it twice.
            headers = response.headers.copy()
            headers.pop("content-encoding", None)
            headers["content-length"] = str(len(content))
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=bytes(content),
                request=response.request,
                extensions=response.extensions,
            )


def _base_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def _slug(url: str) -> str:
    host = (urlsplit(url).hostname or "site").lower()
    host = host.removeprefix("www.")
    value = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    return (value or "site")[:63].rstrip("-")


def config_candidate(
    final_url: str,
    analysis: HtmlAnalysis,
    *,
    feed_url: str | None = None,
    sitemap_url: str | None = None,
    list_url: str | None = None,
) -> dict[str, Any]:
    """Build a disabled candidate; business-only tagset_group remains for confirmation."""
    candidate: dict[str, Any] = {
        "slug": _slug(final_url),
        "name": analysis.title,
        "base_url": _base_url(final_url),
        "enabled": False,
        "render_js": analysis.likely_needs_js,
        "discovery_mode": "html_list",
        "config": {"list_urls": [list_url or final_url]},
        "politeness": {"max_concurrency": 2, "delay_sec": 2},
    }
    if feed_url:
        candidate.update(discovery_mode="rss", config={"feed_url": feed_url})
        return candidate
    if sitemap_url:
        candidate.update(discovery_mode="sitemap", config={"sitemap_url": sitemap_url})
        return candidate
    if analysis.selector_candidates:
        selector = analysis.selector_candidates[0]
        fields = {"url": selector.link_selector, "title": selector.title_selector}
        if selector.published_at_selector:
            fields["published_at"] = selector.published_at_selector
        candidate["config"].update({"item_selector": selector.item_selector, "fields": fields})
    if analysis.pagination_candidates:
        candidate["config"]["pagination"] = analysis.pagination_candidates[0].config
    return candidate


def _article_urls(html: str, page_url: str, analysis: HtmlAnalysis) -> tuple[str, ...]:
    if not analysis.selector_candidates:
        return ()
    candidate = analysis.selector_candidates[0]
    selector, _, attr = candidate.link_selector.rpartition("@")
    if attr != "href":
        return ()
    urls: list[str] = []
    tree = HTMLParser(html)
    for item in tree.css(candidate.item_selector):
        anchors = item.css(selector) if selector else [item]
        for anchor in anchors:
            href = anchor.attributes.get("href")
            if href:
                absolute = urljoin(page_url, href)
                if urlsplit(absolute).scheme in {"http", "https"}:
                    urls.append(absolute)
    return tuple(dict.fromkeys(urls))


def probe_site(
    url: str,
    *,
    limits: ProbeLimits | None = None,
    resolver: Resolver = system_resolver,
    transport: httpx.BaseTransport | None = None,
) -> ProbeReport:
    """Run a bounded, read-only probe; every request uses one globally budgeted SafeFetcher."""
    started = time.monotonic()
    limits = limits or ProbeLimits()
    fetcher = SafeFetcher(limits=limits, resolver=resolver, transport=transport)
    page = fetcher.fetch(url)
    analysis = analyze_html(page.text, page.final_url)
    report = ProbeReport(base_url=_base_url(page.final_url))
    fetched: dict[str, FetchedPage] = {page.requested_url: page, page.final_url: page}
    exhausted = False

    def optional_fetch(target: str) -> FetchedPage | None:
        nonlocal exhausted
        if target in fetched:
            return fetched[target]
        if exhausted:
            return None
        try:
            result = fetcher.fetch(target)
        except (ProbeLimitExceeded, ProbeTimedOut) as exc:
            exhausted = True
            report.notes.append(str(exc))
            return None
        except (ProbeError, SsrfBlocked) as exc:
            report.notes.append(f"candidate fetch failed: {target}: {type(exc).__name__}")
            return None
        fetched[target] = result
        fetched[result.final_url] = result
        return result

    report.add(ProbeFinding("final_url", page.final_url, "high", f"HTTP {page.status_code}"))
    report.add(
        ProbeFinding(
            "page_type",
            analysis.page_type,
            "high" if analysis.confidence >= 0.8 else "medium",
            f"static text length {analysis.text_length}",
        )
    )
    report.add(
        ProbeFinding(
            "render_js",
            analysis.likely_needs_js,
            "medium" if analysis.likely_needs_js else "high",
            "static HTML heuristic only",
        )
    )

    feeds: list[dict[str, Any]] = []
    root_feed = identify_feed(page.text)
    if root_feed:
        feeds.append(
            {
                "url": page.final_url,
                "kind": root_feed,
                "confidence": 1.0,
                "evidence": "input URL has a feed XML root",
            }
        )
    for candidate in feed_candidates(page.text, page.final_url):
        candidate_page = optional_fetch(candidate.url)
        kind = identify_feed(candidate_page.text) if candidate_page else None
        if kind:
            value = candidate.as_dict()
            value.update(
                url=candidate_page.final_url,
                kind=kind,
                confidence=max(candidate.confidence, 0.9),
            )
            feeds.append(value)
    feeds = list({item["url"]: item for item in feeds}.values())
    report.add(
        ProbeFinding(
            "feed",
            feeds[0]["url"] if feeds else None,
            "high" if feeds else "low",
            "validated RSS/Atom XML" if feeds else "no declared or common feed validated",
            tuple(feeds),
        )
    )

    robots_url = urljoin(_base_url(page.final_url), "robots.txt")
    robots_page = optional_fetch(robots_url)
    robots_text = robots_page.text if robots_page else ""
    sitemaps: list[dict[str, Any]] = []
    for candidate in sitemap_candidates(robots_text, page.final_url):
        candidate_page = optional_fetch(candidate.url)
        kind = identify_sitemap(candidate_page.text) if candidate_page else None
        if kind:
            value = candidate.as_dict()
            value.update(
                url=candidate_page.final_url,
                kind=kind,
                confidence=max(candidate.confidence, 0.9),
            )
            sitemaps.append(value)
    sitemaps = list({item["url"]: item for item in sitemaps}.values())
    report.add(
        ProbeFinding(
            "sitemap",
            sitemaps[0]["url"] if sitemaps else None,
            "high" if sitemaps else "low",
            "validated sitemap XML" if sitemaps else "no robots/common sitemap validated",
            tuple(sitemaps),
        )
    )

    pages: list[tuple[FetchedPage, HtmlAnalysis, str]] = [(page, analysis, "input page")]
    queued = [
        (item.url, 1, item.evidence) for item in navigation_candidates(page.text, page.final_url)
    ]
    visited = {page.final_url}
    while queued and not exhausted:
        target, depth, evidence = queued.pop(0)
        if target in visited:
            continue
        visited.add(target)
        candidate_page = optional_fetch(target)
        if candidate_page is None:
            continue
        candidate_analysis = analyze_html(candidate_page.text, candidate_page.final_url)
        pages.append((candidate_page, candidate_analysis, evidence))
        if depth < 2:
            queued.extend(
                (item.url, depth + 1, item.evidence)
                for item in navigation_candidates(candidate_page.text, candidate_page.final_url)
                if item.url not in visited
            )

    list_pages: list[dict[str, Any]] = []
    ranked_analyses: dict[str, HtmlAnalysis] = {}
    for candidate_page, candidate_analysis, evidence in pages:
        if candidate_analysis.page_type not in {"html_list", "possible_list", "navigation"}:
            continue
        pagination = list(candidate_analysis.pagination_candidates)
        if pagination and pagination[0].sample_url:
            sample_page = optional_fetch(pagination[0].sample_url)
            if sample_page:
                pagination[0] = infer_pagination_direction(
                    candidate_page.text, sample_page.text, pagination[0]
                )
        ranked_analyses[candidate_page.final_url] = replace(
            candidate_analysis, pagination_candidates=tuple(pagination)
        )
        list_pages.append(
            {
                "url": candidate_page.final_url,
                "title": candidate_analysis.title,
                "page_type": candidate_analysis.page_type,
                "confidence": candidate_analysis.confidence,
                "matches": (
                    candidate_analysis.selector_candidates[0].matches
                    if candidate_analysis.selector_candidates
                    else 0
                ),
                "sample": (
                    candidate_analysis.selector_candidates[0].samples[0]
                    if candidate_analysis.selector_candidates
                    and candidate_analysis.selector_candidates[0].samples
                    else None
                ),
                "evidence": evidence,
                "selector_candidates": tuple(
                    item.as_dict() for item in candidate_analysis.selector_candidates
                ),
                "pagination_candidates": tuple(item.as_dict() for item in pagination),
            }
        )
    list_pages.sort(
        key=lambda item: (item["page_type"] == "html_list", item["confidence"], item["matches"]),
        reverse=True,
    )
    report.add(
        ProbeFinding(
            "list_pages",
            list_pages[0]["url"] if list_pages else None,
            "medium" if list_pages else "failed",
            (
                "ranked static list/navigation candidates"
                if list_pages
                else "no likely list page found"
            ),
            tuple(list_pages[:8]),
        )
    )

    best_page, best_analysis = page, analysis
    if list_pages:
        best_page = fetched[list_pages[0]["url"]]
        best_analysis = ranked_analyses[best_page.final_url]
    selector_dicts = tuple(item.as_dict() for item in best_analysis.selector_candidates)
    report.add(
        ProbeFinding(
            "item_selector",
            selector_dicts[0]["item_selector"] if selector_dicts else None,
            "medium" if selector_dicts else "failed",
            selector_dicts[0]["evidence"] if selector_dicts else "no repeated link structure found",
            selector_dicts,
        )
    )
    pagination_dicts = tuple(item.as_dict() for item in best_analysis.pagination_candidates)
    report.add(
        ProbeFinding(
            "pagination",
            pagination_dicts[0]["config"] if pagination_dicts else {"mode": "none"},
            "medium" if pagination_dicts else "low",
            pagination_dicts[0]["evidence"] if pagination_dicts else "no pagination links found",
            pagination_dicts,
        )
    )

    article_urls: list[str] = []
    for candidate_page, candidate_analysis, _ in pages:
        if candidate_analysis.page_type in {"html_list", "possible_list"}:
            article_urls.extend(
                _article_urls(candidate_page.text, candidate_page.final_url, candidate_analysis)
            )
    article_urls = list(dict.fromkeys(article_urls))[:24]
    pairs = dual_form_pairs(article_urls)
    sample_order = list(dict.fromkeys(url for pair in pairs for url in pair))
    sample_order.extend(url for url in article_urls if url not in sample_order)
    article_samples: list[tuple[str, str]] = []
    for article_url in sample_order[: limits.sample_articles]:
        article_page = optional_fetch(article_url)
        if article_page:
            article_samples.append((article_page.final_url, article_page.text))
    sample_map = dict(article_samples)
    dual_forms = tuple(
        confirmed.as_dict()
        for left, right in pairs
        if left in sample_map
        and right in sample_map
        and (confirmed := confirm_dual_form(left, sample_map[left], right, sample_map[right]))
    )
    report.add(
        ProbeFinding(
            "url_canonical.rules",
            None,
            "medium" if dual_forms else "low",
            (
                "same-article dual URL forms require confirmation"
                if dual_forms
                else "no confirmed dual forms"
            ),
            dual_forms,
        )
    )
    attachment_candidates = detect_attachment_patterns(article_samples)
    attachment_dicts = tuple(item.as_dict() for item in attachment_candidates)
    report.add(
        ProbeFinding(
            "attachments.link_patterns",
            [item["pattern"] for item in attachment_dicts],
            "medium" if attachment_dicts else "low",
            "patterns inferred from bounded article HTML; linked files were not fetched",
            attachment_dicts,
        )
    )

    candidate = config_candidate(
        page.final_url,
        best_analysis,
        feed_url=feeds[0]["url"] if feeds else None,
        sitemap_url=sitemaps[0]["url"] if sitemaps else None,
        list_url=best_page.final_url,
    )
    if attachment_dicts:
        candidate["attachments"] = {"link_patterns": [item["pattern"] for item in attachment_dicts]}
    report.add(
        ProbeFinding(
            "config_candidate",
            candidate,
            "medium",
            "disabled dry-run candidate; tagset_group still requires user confirmation",
        )
    )
    if page.redirects:
        report.notes.append(f"validated {len(page.redirects)} redirect hop(s)")
    report.notes.append(f"used {fetcher.pages_fetched}/{limits.max_pages} page requests")
    report.duration_ms = int((time.monotonic() - started) * 1000)
    return report

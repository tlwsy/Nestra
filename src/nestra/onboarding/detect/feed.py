"""Static resource candidates and conservative RSS/Atom/sitemap validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from selectolax.parser import HTMLParser

_FEED_TYPES = {"application/atom+xml", "application/rss+xml"}
_COMMON_FEEDS = ("feed", "rss.xml", "atom.xml", "feed.xml")


@dataclass(frozen=True, slots=True)
class ResourceCandidate:
    url: str
    kind: str
    confidence: float
    evidence: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def feed_candidates(html: str, page_url: str) -> tuple[ResourceCandidate, ...]:
    """Return declared feeds first, then conventional URLs as unverified candidates."""
    found: list[ResourceCandidate] = []
    seen: set[str] = set()
    for node in HTMLParser(html).css("link[href]"):
        rel = node.attributes.get("rel", "").lower().split()
        media_type = node.attributes.get("type", "").lower().split(";", 1)[0].strip()
        if "alternate" not in rel or media_type not in _FEED_TYPES:
            continue
        url = urljoin(page_url, node.attributes["href"])
        if urlsplit(url).scheme in {"http", "https"} and url not in seen:
            seen.add(url)
            kind = "atom" if "atom" in media_type else "rss"
            found.append(ResourceCandidate(url, kind, 0.98, f"declared as {media_type}"))
    for path in _COMMON_FEEDS:
        url = urljoin(_origin(page_url), path)
        if url not in seen:
            seen.add(url)
            found.append(ResourceCandidate(url, "feed", 0.35, f"common /{path} path"))
    return tuple(found)


def sitemap_candidates(robots_text: str, page_url: str) -> tuple[ResourceCandidate, ...]:
    """Read robots Sitemap directives and include the conventional path as a candidate."""
    found: list[ResourceCandidate] = []
    seen: set[str] = set()
    for line in robots_text.splitlines():
        key, separator, value = line.partition(":")
        value = value.split("#", 1)[0].strip()
        if not separator or key.strip().lower() != "sitemap" or not value:
            continue
        url = urljoin(page_url, value)
        if urlsplit(url).scheme in {"http", "https"} and url not in seen:
            seen.add(url)
            found.append(ResourceCandidate(url, "sitemap", 0.98, "robots.txt Sitemap directive"))
    common = urljoin(_origin(page_url), "sitemap.xml")
    if common not in seen:
        found.append(ResourceCandidate(common, "sitemap", 0.4, "common /sitemap.xml path"))
    return tuple(found)


def identify_feed(document: str) -> str | None:
    """Return rss/atom only when the response has a matching XML root element."""
    try:
        root = ElementTree.fromstring(document.lstrip("\ufeff \t\r\n"))
    except (DefusedXmlException, ElementTree.ParseError, ValueError):
        return None
    name = root.tag.rsplit("}", 1)[-1].lower()
    if name in {"rss", "rdf"}:
        return "rss"
    return "atom" if name == "feed" else None


def identify_sitemap(document: str) -> str | None:
    """Return urlset/sitemapindex only for a valid XML root."""
    try:
        root = ElementTree.fromstring(document.lstrip("\ufeff \t\r\n"))
    except (DefusedXmlException, ElementTree.ParseError, ValueError):
        return None
    name = root.tag.rsplit("}", 1)[-1].lower()
    return name if name in {"urlset", "sitemapindex"} else None

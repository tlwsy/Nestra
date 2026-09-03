"""Same-site navigation candidates used by the bounded list-page probe."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

_LIST_WORDS = re.compile(
    r"通知|公告|新闻|动态|资讯|列表|博客|文章|news|notice|blog|posts?|archive|updates?",
    re.I,
)


@dataclass(frozen=True, slots=True)
class NavigationCandidate:
    url: str
    title: str
    score: float
    evidence: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def navigation_candidates(html: str, page_url: str) -> tuple[NavigationCandidate, ...]:
    """Rank explicit same-site links whose text or URL suggests a list/archive page."""
    origin_host = urlsplit(page_url).hostname
    found: dict[str, NavigationCandidate] = {}
    for anchor in HTMLParser(html).css("a[href]"):
        href = anchor.attributes.get("href", "").strip()
        absolute = urljoin(page_url, href)
        parsed = urlsplit(absolute)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != origin_host:
            continue
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
        title = (anchor.attributes.get("title") or anchor.text(separator=" ", strip=True))[:100]
        text_hit = bool(_LIST_WORDS.search(title))
        url_hit = bool(_LIST_WORDS.search(parsed.path + "?" + parsed.query))
        if not text_hit and not url_hit:
            continue
        score = 0.65 * text_hit + 0.35 * url_hit
        evidence = ", ".join(
            part for part, hit in (("link text", text_hit), ("URL", url_hit)) if hit
        )
        candidate = NavigationCandidate(url, title, round(score, 2), f"list keyword in {evidence}")
        current = found.get(url)
        if current is None or candidate.score > current.score:
            found[url] = candidate
    return tuple(sorted(found.values(), key=lambda item: item.score, reverse=True)[:16])

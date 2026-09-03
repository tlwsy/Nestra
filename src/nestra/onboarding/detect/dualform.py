"""Conservative article URL dual-form candidates."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlsplit

from selectolax.parser import HTMLParser

_NUMBER = re.compile(r"\d+")


@dataclass(frozen=True, slots=True)
class DualFormCandidate:
    urls: tuple[str, str]
    confidence: float
    parameter_mapping: tuple[tuple[str, str], ...]
    evidence: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def dual_form_pairs(urls: list[str] | tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Pair path/query forms only when they carry the same bounded numeric identity."""
    unique = tuple(dict.fromkeys(urls))
    pairs: list[tuple[str, str]] = []
    for index, left in enumerate(unique):
        left_parsed = urlsplit(left)
        for right in unique[index + 1 :]:
            right_parsed = urlsplit(right)
            if right_parsed.hostname != left_parsed.hostname:
                continue
            path, query = (
                (left_parsed, right_parsed)
                if not left_parsed.query and right_parsed.query
                else (right_parsed, left_parsed)
            )
            path_values = tuple(_NUMBER.findall(path.path))
            query_values = tuple(
                value for _, value in parse_qsl(query.query) if value in path_values
            )
            if path_values and path_values == query_values:
                pairs.append((left, right))
    return tuple(pairs)


def _article_text(html: str) -> tuple[str, str]:
    tree = HTMLParser(html)
    title_node = tree.css_first("h1") or tree.css_first("title")
    root = tree.css_first("article, main") or tree.body or tree
    title = title_node.text(separator=" ", strip=True) if title_node else ""
    text = root.text(separator=" ", strip=True)[:20_000]
    return title, text


def confirm_dual_form(
    left_url: str,
    left_html: str,
    right_url: str,
    right_html: str,
) -> DualFormCandidate | None:
    """Confirm a candidate only when title and substantial article text agree."""
    left_title, left_text = _article_text(left_html)
    right_title, right_text = _article_text(right_html)
    if not left_title or left_title != right_title or min(len(left_text), len(right_text)) < 80:
        return None
    similarity = SequenceMatcher(None, left_text, right_text, autojunk=False).ratio()
    if similarity < 0.85:
        return None

    path_url, query_url = (
        (left_url, right_url) if not urlsplit(left_url).query else (right_url, left_url)
    )
    path_values = _NUMBER.findall(urlsplit(path_url).path)
    query = parse_qsl(urlsplit(query_url).query)
    remaining = list(path_values)
    mapping = []
    for key, value in query:
        if value in remaining:
            mapping.append((key, value))
            remaining.remove(value)
    return DualFormCandidate(
        (left_url, right_url),
        round(min(0.99, 0.75 + similarity * 0.24), 3),
        tuple(mapping),
        f"same title and {similarity:.0%} article-text similarity; review before canonicalizing",
    )

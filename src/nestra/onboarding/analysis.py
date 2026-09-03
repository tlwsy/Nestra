"""Static HTML classification and confidence-ranked configuration suggestions."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date
from statistics import median
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser, Node

_DATE_RE = re.compile(r"(?:19|20)\d{2}[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?")
_SAFE_CLASS_RE = re.compile(r"^-?[_a-zA-Z][_a-zA-Z0-9-]*$")
_NEXT_TEXT = re.compile(r"^(?:next|next page|下一页|下页|后一页|后页|›|»|>)$", re.I)
_LIST_WORDS = re.compile(r"通知|公告|新闻|列表|blog|posts?|news|archive", re.I)


@dataclass(frozen=True, slots=True)
class SelectorCandidate:
    item_selector: str
    link_selector: str
    title_selector: str
    published_at_selector: str | None
    confidence: float
    matches: int
    samples: tuple[str, ...]
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PaginationCandidate:
    config: dict[str, Any]
    confidence: float
    evidence: str
    sample_url: str | None = None
    direction: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HtmlAnalysis:
    title: str
    page_type: str
    confidence: float
    likely_needs_js: bool
    text_length: int
    selector_candidates: tuple[SelectorCandidate, ...]
    pagination_candidates: tuple[PaginationCandidate, ...]


def _css_part(node: Node) -> str:
    classes = [
        value
        for value in node.attributes.get("class", "").split()
        if _SAFE_CLASS_RE.fullmatch(value)
    ][:2]
    return node.tag + "".join(f".{value}" for value in classes)


def _same_site(href: str, base_url: str) -> bool:
    target = urlsplit(urljoin(base_url, href))
    return target.scheme in {"http", "https"} and target.hostname == urlsplit(base_url).hostname


def _date_selector(rows: tuple[tuple[Node, Node], ...]) -> tuple[str | None, float]:
    selectors: Counter[str] = Counter()
    dated_rows = 0
    for parent, _ in rows:
        found: set[str] = set()
        for node in parent.css("*"):
            if node.mem_id == parent.mem_id:
                continue
            text = node.text(separator=" ", strip=True)
            dated_child = any(
                child.mem_id != node.mem_id
                and _DATE_RE.search(child.text(separator=" ", strip=True))
                for child in node.css("*")
            )
            if _DATE_RE.search(text) and not dated_child:
                found.add(_css_part(node))
        if found:
            dated_rows += 1
            selectors.update(found)
    if not selectors:
        return None, 0.0
    selector, count = selectors.most_common(1)[0]
    return selector, count / len(rows) if rows else 0.0


def induce_selectors(tree: HTMLParser, base_url: str) -> tuple[SelectorCandidate, ...]:
    """Rank repeated link-bearing DOM structures as list-item selectors."""
    groups: dict[tuple[str, str], list[tuple[Node, Node]]] = defaultdict(list)
    for anchor in tree.css("a[href]"):
        parent = anchor.parent
        for _ in range(3):
            if parent is None or parent.tag in {"html", "body"}:
                break
            groups[(_css_part(parent), _css_part(anchor))].append((parent, anchor))
            parent = parent.parent

    candidates: list[SelectorCandidate] = []
    for (item_selector, link_selector), pairs in groups.items():
        unique: dict[int, tuple[Node, Node]] = {
            parent.mem_id: (parent, anchor) for parent, anchor in pairs
        }
        if not 3 <= len(unique) <= 100:
            continue
        try:
            matches = len(tree.css(item_selector))
        except ValueError:
            continue
        if matches != len(unique):
            continue

        rows = tuple(unique.values())
        texts = tuple(
            (anchor.attributes.get("title") or anchor.text(strip=True))[:100]
            for _, anchor in rows
            if anchor.attributes.get("title") or anchor.text(strip=True)
        )
        if not texts:
            continue
        lengths = [len(text) for text in texts]
        useful_text = sum(4 <= length <= 120 for length in lengths) / len(lengths)
        same_site = sum(
            _same_site(anchor.attributes.get("href", ""), base_url) for _, anchor in rows
        ) / len(rows)
        published_at_selector, date_ratio = _date_selector(rows)
        specificity = 1.0 if "." in item_selector else 0.4
        quantity = min(len(rows) / 8, 1.0)
        confidence = round(
            0.30 * quantity
            + 0.25 * useful_text
            + 0.20 * same_site
            + 0.15 * date_ratio
            + 0.10 * specificity,
            3,
        )
        long_titles = sum(
            len(anchor.attributes.get("title", "")) > len(anchor.text(strip=True))
            and (
                anchor.text(strip=True).endswith(("...", "…"))
                or len(anchor.attributes.get("title", "")) >= len(anchor.text(strip=True)) * 1.2
            )
            for _, anchor in rows
        )
        title_selector = f"{link_selector}@title" if long_titles > len(rows) / 2 else link_selector
        candidates.append(
            SelectorCandidate(
                item_selector=item_selector,
                link_selector=f"{link_selector}@href",
                title_selector=title_selector,
                published_at_selector=published_at_selector,
                confidence=confidence,
                matches=len(rows),
                samples=texts[:3],
                evidence=(
                    f"{len(rows)} repeated items; {date_ratio:.0%} contain dates; "
                    f"{same_site:.0%} links stay on site"
                ),
            )
        )
    candidates.sort(key=lambda item: (item.confidence, item.matches), reverse=True)
    return tuple(candidates[:5])


def _page_link_selector(anchor: Node) -> str:
    rel = anchor.attributes.get("rel", "").lower().split()
    if "next" in rel:
        return 'a[rel="next"]'
    return _css_part(anchor)


def suggest_pagination(tree: HTMLParser, base_url: str) -> tuple[PaginationCandidate, ...]:
    """Infer pagination shape from explicit links without guessing temporal direction."""
    suggestions: list[PaginationCandidate] = []
    numbered: list[tuple[str, int]] = []
    for anchor in tree.css("a[href]"):
        href = anchor.attributes.get("href", "")
        text = anchor.text(strip=True)
        absolute = urljoin(base_url, href)
        rel = anchor.attributes.get("rel", "").lower().split()
        if "next" in rel or _NEXT_TEXT.fullmatch(text):
            suggestions.append(
                PaginationCandidate(
                    {"mode": "next_link", "next_selector": _page_link_selector(anchor)},
                    0.95 if "next" in rel else 0.82,
                    "explicit rel=next" if "next" in rel else f"link text is {text!r}",
                    absolute,
                )
            )
        if text.isdigit():
            numbered.append((absolute, int(text)))

    query_groups: dict[str, list[tuple[int, str]]] = defaultdict(list)
    path_groups: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for absolute, page_number in numbered:
        parsed = urlsplit(absolute)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if value.isdigit() and int(value) == page_number:
                query_groups[key].append((page_number, absolute))
        template_path, replacements = re.subn(
            rf"(?<!\d){page_number}(?!\d)", "{page}", parsed.path, count=1
        )
        if replacements:
            query = urlencode(parse_qsl(parsed.query, keep_blank_values=True))
            template = urlunsplit((parsed.scheme, parsed.netloc, template_path, query, ""))
            path_groups[template].append((page_number, absolute))

    for param, values in query_groups.items():
        pages = {page for page, _ in values}
        if len(pages) >= 2:
            sample = min(values, key=lambda item: item[0])[1]
            suggestions.append(
                PaginationCandidate(
                    {"mode": "query_param", "param": param},
                    0.78,
                    f"numeric page links vary query parameter {param!r}",
                    sample,
                )
            )
    for template, values in path_groups.items():
        pages = {page for page, _ in values}
        if len(pages) >= 2:
            sample = min(values, key=lambda item: item[0])[1]
            suggestions.append(
                PaginationCandidate(
                    {"mode": "url_template", "template": template, "max_page": max(pages)},
                    0.74,
                    "numeric page links share one path template",
                    sample,
                )
            )

    deduplicated: dict[tuple[tuple[str, str], ...], PaginationCandidate] = {}
    for suggestion in suggestions:
        key = tuple(sorted((name, str(value)) for name, value in suggestion.config.items()))
        current = deduplicated.get(key)
        if current is None or suggestion.confidence > current.confidence:
            deduplicated[key] = suggestion
    return tuple(sorted(deduplicated.values(), key=lambda item: item.confidence, reverse=True)[:5])


def _date_ordinals(html: str) -> list[int]:
    values: list[int] = []
    for raw in _DATE_RE.findall(HTMLParser(html).text(separator=" ", strip=True)):
        parts = [int(value) for value in re.findall(r"\d+", raw)]
        try:
            values.append(date(parts[0], parts[1], parts[2] if len(parts) > 2 else 1).toordinal())
        except (ValueError, IndexError):
            continue
    return values


def infer_pagination_direction(
    entry_html: str,
    sample_html: str,
    candidate: PaginationCandidate,
) -> PaginationCandidate:
    """Annotate direction only when both pages contain distinguishable date evidence."""
    entry_dates = _date_ordinals(entry_html)
    sample_dates = _date_ordinals(sample_html)
    if not entry_dates or not sample_dates or median(entry_dates) == median(sample_dates):
        return candidate
    newest_first = median(entry_dates) > median(sample_dates)
    direction = "newer_to_older" if newest_first else "older_to_newer"
    config = dict(candidate.config)
    if config.get("mode") == "url_template" and config.get("max_page") and newest_first:
        config["order"] = "desc_index"
    else:
        config["order"] = "asc"
    return replace(
        candidate,
        config=config,
        confidence=round(min(0.98, candidate.confidence + 0.12), 3),
        evidence=f"{candidate.evidence}; page date medians show {direction}",
        direction=direction,
    )


def analyze_html(html: str, base_url: str) -> HtmlAnalysis:
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else urlsplit(base_url).hostname or base_url
    body = tree.body
    text = body.text(separator=" ", strip=True) if body else tree.text(separator=" ", strip=True)
    selectors = induce_selectors(tree, base_url)
    pagination = suggest_pagination(tree, base_url)
    scripts = len(tree.css("script"))
    roots = len(tree.css("[id=root], [id=app], [data-reactroot]"))
    likely_js = len(text) < 120 and (scripts >= 2 or roots > 0)
    date_count = len(_DATE_RE.findall(text))
    link_count = len(tree.css("a[href]"))
    list_named = bool(_LIST_WORDS.search(title + " " + urlsplit(base_url).path))
    if selectors and (date_count >= 3 or list_named):
        page_type, confidence = "html_list", selectors[0].confidence
    elif likely_js:
        page_type, confidence = "likely_spa", 0.8
    elif link_count >= 8 and date_count == 0:
        page_type, confidence = "navigation", 0.75
    elif len(text) >= 500 or len(tree.css("article, main, p")) >= 3:
        page_type, confidence = "article", 0.7
    elif selectors:
        page_type, confidence = "possible_list", selectors[0].confidence * 0.8
    else:
        page_type, confidence = "unknown", 0.35
    return HtmlAnalysis(
        title=title,
        page_type=page_type,
        confidence=confidence,
        likely_needs_js=likely_js,
        text_length=len(text),
        selector_candidates=selectors,
        pagination_candidates=pagination,
    )

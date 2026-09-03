"""Infer reviewable attachment URL patterns from already-fetched article samples."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser

_EXTENSIONS = {
    "csv",
    "doc",
    "docx",
    "gz",
    "pdf",
    "ppt",
    "pptx",
    "rar",
    "tar",
    "xls",
    "xlsx",
    "zip",
}
_DOWNLOAD_NAME = re.compile(r"(?:attach|download|file)", re.I)


@dataclass(frozen=True, slots=True)
class AttachmentPatternCandidate:
    pattern: str
    confidence: float
    matches: int
    samples: tuple[str, ...]
    evidence: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def detect_attachment_patterns(
    samples: list[tuple[str, str]] | tuple[tuple[str, str], ...],
) -> tuple[AttachmentPatternCandidate, ...]:
    """Group obvious file extensions and download endpoints; never fetch linked files."""
    groups: dict[str, list[str]] = defaultdict(list)
    for page_url, html in samples:
        for anchor in HTMLParser(html).css("a[href]"):
            url = urljoin(page_url, anchor.attributes.get("href", ""))
            parsed = urlsplit(url)
            filename = parsed.path.rsplit("/", 1)[-1]
            suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if suffix in _EXTENSIONS:
                groups[rf"\.{re.escape(suffix)}(?:$|[?#])"].append(url)
            elif _DOWNLOAD_NAME.search(filename):
                groups[re.escape(filename)].append(url)
    candidates = [
        AttachmentPatternCandidate(
            pattern,
            0.9 if pattern.startswith(r"\.") else 0.7,
            len(set(urls)),
            tuple(dict.fromkeys(urls))[:3],
            "file extension" if pattern.startswith(r"\.") else "download-like endpoint name",
        )
        for pattern, urls in groups.items()
    ]
    return tuple(sorted(candidates, key=lambda item: (item.confidence, item.matches), reverse=True))

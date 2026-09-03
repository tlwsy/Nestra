"""领域数据类。

这些是**层间传递**用的类型，与 DB 行和配置模型都不同：
- `config.py` 的模型描述用户写的配置
- 这里的类型描述流水线中流动的数据
- `storage/repositories/` 负责两者与 DB 行的互转

用 frozen dataclass：流水线里的数据被下游改动是很难查的 bug。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ArticleStatus(StrEnum):
    """文章状态机。

    DISCOVERED → FETCHED → EXTRACTED → TAGGED → NOTIFIED
    任何环节可转 FAILED（可重试）或 SKIPPED（终态，如正文过短）。
    """

    DISCOVERED = "DISCOVERED"
    FETCHED = "FETCHED"
    EXTRACTED = "EXTRACTED"
    TAGGED = "TAGGED"
    NOTIFIED = "NOTIFIED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"

    @property
    def is_terminal(self) -> bool:
        return self in (ArticleStatus.NOTIFIED, ArticleStatus.SKIPPED)


class AttachmentStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class DiscoveredItem:
    """发现阶段的产物：一条待抓取的线索。

    `content_html` 用于 RSS 全文输出的场景，可跳过一次请求。
    """

    url: str
    title: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    content_html: str | None = None
    source_page: str | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """一次 HTTP 抓取的结果。"""

    url: str
    final_url: str
    status_code: int
    html: str
    encoding: str
    etag: str | None = None
    last_modified: datetime | None = None
    elapsed_ms: int = 0
    from_cache: bool = False


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """提取阶段发现的附件线索，尚未下载。"""

    source_url: str
    filename: str | None = None
    link_text: str | None = None


@dataclass(frozen=True, slots=True)
class ArticleText:
    """提取阶段的产物。"""

    title: str
    content_text: str
    content_html: str
    summary: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    lang: str | None = None
    attachments: tuple[AttachmentRef, ...] = ()

    @property
    def word_count(self) -> int:
        return len(self.content_text)


@dataclass(frozen=True, slots=True)
class TagAssignment:
    """一条打标结果。

    `backend` 记录来源（`llm:deepseek:deepseek-chat` 或 `local:bge-small`），
    便于事后评估不同后端的质量差异。
    """

    tag_slug: str
    confidence: float
    backend: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TagsetEntry:
    """冻结标签集中的一个标签。"""

    slug: str
    name: str
    description: str = ""
    keywords: tuple[str, ...] = ()
    threshold: float = 0.35
    centroid: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class Tagset:
    """某分组的冻结标签集。"""

    group_slug: str
    version: str
    build_mode: str
    checksum: str
    entries: tuple[TagsetEntry, ...]
    frozen_at: datetime | None = None

    @property
    def slugs(self) -> frozenset[str]:
        """白名单。LLM 返回集合外的标签一律丢弃。"""
        return frozenset(e.slug for e in self.entries)

    @property
    def has_centroids(self) -> bool:
        """决定本地兜底是否可用。LLM 模式生成的标签集初始没有质心。"""
        return bool(self.entries) and all(e.centroid for e in self.entries)

    def get(self, slug: str) -> TagsetEntry | None:
        return next((e for e in self.entries if e.slug == slug), None)


@dataclass(frozen=True, slots=True)
class ProbeFinding:
    """向导探测出的单个配置项及其置信度。

    `confidence` 决定 UI 呈现方式：high 折叠，medium 展开待确认，
    low/failed 高亮要求人工处理。
    """

    key: str
    value: Any
    confidence: str  # high | medium | low | failed
    evidence: str = ""
    candidates: tuple[Any, ...] = ()

    @property
    def needs_review(self) -> bool:
        return self.confidence in ("low", "failed")


@dataclass
class ProbeReport:
    """探测总结果。可变：探测分阶段填充。"""

    base_url: str
    findings: list[ProbeFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def add(self, finding: ProbeFinding) -> None:
        self.findings.append(finding)

    def get(self, key: str) -> ProbeFinding | None:
        return next((f for f in self.findings if f.key == key), None)

    @property
    def review_required(self) -> list[ProbeFinding]:
        return [f for f in self.findings if f.needs_review]

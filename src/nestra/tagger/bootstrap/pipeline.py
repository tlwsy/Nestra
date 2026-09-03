"""Standalone, dependency-injectable tagset bootstrap orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...core.errors import TagsetNotReady
from ...storage.db import Database
from .curate import CurationResult, curate, render_report
from .freeze import freeze_tagset
from .llm_induct import (
    CandidateTag,
    Inducer,
    batch_prompt,
    invoke_inducer,
    merge_duplicates,
    merge_prompt,
)


@dataclass(frozen=True, slots=True)
class BootstrapOptions:
    group: str
    group_name: str | None = None
    mode: str = "llm"
    batch_size: int = 40
    min_tags: int = 30
    max_tags: int = 80
    min_cluster_docs: int = 5
    min_documents: int = 1
    max_documents: int = 2000
    drop_too_broad_ratio: float = 0.4
    require_manual_review: bool = False
    reviewed: bool = False
    embedding_model: str | None = None

    def validate(self) -> None:
        if self.mode not in {"llm", "embedding"}:
            raise TagsetNotReady("bootstrap mode must be llm or embedding")
        if self.batch_size < 1 or not 1 <= self.min_tags <= self.max_tags <= 80:
            raise TagsetNotReady("require batch_size >= 1 and 1 <= min_tags <= max_tags <= 80")
        if (
            self.min_cluster_docs < 1
            or self.min_documents < 1
            or self.max_documents < self.min_documents
            or not 0 < self.drop_too_broad_ratio <= 1
        ):
            raise TagsetNotReady("invalid curation limits")


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    document: Mapping[str, Any]
    report_path: Path
    tagset_path: Path | None
    frozen: bool


def load_historical_articles(
    db: Database, group: str, *, limit: int = 2000
) -> list[dict[str, Any]]:
    rows = reversed(
        db.query(
            "SELECT a.id, a.title, a.summary, a.content_text FROM articles a "
            "JOIN sites s ON s.id=a.site_id "
            "JOIN tagset_groups g ON g.id=s.tagset_group_id "
            "WHERE g.slug=? AND a.content_text IS NOT NULL AND trim(a.content_text) != '' "
            "AND a.status IN ('EXTRACTED','TAGGED','NOTIFIED') ORDER BY a.id DESC LIMIT ?",
            (group, limit),
        )
    )
    return [
        {
            "id": row["id"],
            "title": row["title"] or "",
            "summary": row["summary"] or row["content_text"][:1200],
        }
        for row in rows
    ]


async def _llm_candidates(
    articles: Sequence[Mapping[str, Any]],
    options: BootstrapOptions,
    inducer: Inducer,
) -> tuple[list[CandidateTag], list[str]]:
    candidates: list[CandidateTag] = []
    notes: list[str] = []
    for start in range(0, len(articles), options.batch_size):
        batch = articles[start : start + options.batch_size]
        induced = await invoke_inducer(inducer, batch_prompt(batch, max_tags=options.max_tags))
        allowed_ids = {int(article["id"]) for article in batch}
        if any(set(tag.article_ids) - allowed_ids for tag in induced):
            raise TagsetNotReady("inducer returned article_ids outside its batch")
        candidates.extend(induced)
    candidates, batch_notes = merge_duplicates(candidates)
    notes.extend(batch_notes)
    if len(articles) > options.batch_size:
        allowed_ids = {int(article["id"]) for article in articles}
        merge_size = options.max_tags * 4
        while len(candidates) > merge_size:
            merged_round: list[CandidateTag] = []
            for start in range(0, len(candidates), merge_size):
                chunk = candidates[start : start + merge_size]
                merged = await invoke_inducer(
                    inducer,
                    merge_prompt(chunk, min_tags=1, max_tags=options.max_tags),
                )
                if len(merged) > options.max_tags:
                    raise TagsetNotReady("merge inducer returned too many tags")
                merged_round.extend(merged)
            candidates, round_notes = merge_duplicates(merged_round)
            notes.extend(round_notes)
        candidates = await invoke_inducer(
            inducer,
            merge_prompt(candidates, min_tags=options.min_tags, max_tags=options.max_tags),
        )
        if len(candidates) > options.max_tags:
            raise TagsetNotReady("merge inducer returned too many tags")
        if any(set(tag.article_ids) - allowed_ids for tag in candidates):
            raise TagsetNotReady("merge inducer returned unknown article_ids")
    candidates, final_notes = merge_duplicates(candidates)
    notes.extend(final_notes)
    return candidates, notes


def _tag_document(options: BootstrapOptions, result: CurationResult) -> dict[str, Any]:
    fingerprint = hashlib.sha256(
        json.dumps(
            [asdict(tag) for tag in result.tags],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:6]
    version = f"{datetime.now(UTC):%Y-%m-%d}-{fingerprint}"
    return {
        "group": options.group,
        "group_name": options.group_name or options.group,
        "tagset_version": version,
        "build_mode": options.mode,
        "embedding_model": (
            options.embedding_model or "BAAI/bge-small-zh-v1.5"
            if options.mode == "embedding"
            else None
        ),
        "embedding_dim": next(
            (len(tag.centroid) for tag in result.tags if tag.centroid is not None), None
        ),
        "tags": [
            {
                "slug": tag.slug,
                "name": tag.name,
                "description": tag.description,
                "keywords": list(tag.keywords),
                "threshold": 0.35,
                "centroid": list(tag.centroid) if tag.centroid is not None else None,
                "representative_article_ids": list(tag.article_ids),
            }
            for tag in result.tags
        ],
    }


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


async def bootstrap_tagset(
    db: Database,
    output_root: Path,
    options: BootstrapOptions,
    *,
    inducer: Inducer | None = None,
    articles: Sequence[Mapping[str, Any]] | None = None,
) -> BootstrapResult:
    """Build one group; tests inject ``inducer`` and can inject a tiny corpus."""
    options.validate()
    corpus = (
        list(articles)[: options.max_documents]
        if articles is not None
        else load_historical_articles(db, options.group, limit=options.max_documents)
    )
    if len(corpus) < options.min_documents:
        raise TagsetNotReady(
            f"group {options.group!r} has {len(corpus)} historical articles; "
            f"minimum is {options.min_documents}"
        )
    if options.mode == "embedding":
        from .cluster import embedding_candidates

        candidates = await embedding_candidates(corpus, options, inducer)
        induction_notes: list[str] = []
    else:
        if inducer is None:
            raise TagsetNotReady("llm bootstrap needs an async inducer or configured providers")
        candidates, induction_notes = await _llm_candidates(corpus, options, inducer)

    curated = curate(
        candidates,
        document_count=len(corpus),
        min_cluster_docs=options.min_cluster_docs,
        max_tags=options.max_tags,
        drop_too_broad_ratio=options.drop_too_broad_ratio,
    )
    curated = CurationResult(curated.tags, (*induction_notes, *curated.notes))
    group_dir = output_root / options.group
    report_path = group_dir / "tagset_report.md"

    def write_report(status: str) -> None:
        _atomic_text(
            report_path,
            render_report(
                group=options.group,
                mode=options.mode,
                document_count=len(corpus),
                result=curated,
                status=status,
            ),
        )

    if len(curated.tags) < options.min_tags:
        write_report(f"rejected — {len(curated.tags)} tags below minimum {options.min_tags}")
        raise TagsetNotReady(
            f"curation produced {len(curated.tags)} tags; minimum is {options.min_tags}. "
            "Lower --min-tags only after reviewing the report/input quality; tags are never padded."
        )

    document = _tag_document(options, curated)
    if options.require_manual_review and not options.reviewed:
        draft_path = group_dir / "tags.draft.json"
        _atomic_text(draft_path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        write_report("draft — explicit review required")
        return BootstrapResult(document, report_path, None, False)

    tagset_path = group_dir / "tags.json"
    frozen = freeze_tagset(document, tagset_path, db=db)
    write_report("frozen")
    return BootstrapResult(frozen, report_path, tagset_path, True)

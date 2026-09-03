"""Deterministic final curation and the bootstrap self-check report."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .llm_induct import CandidateTag, merge_duplicates


@dataclass(frozen=True, slots=True)
class CurationResult:
    tags: tuple[CandidateTag, ...]
    notes: tuple[str, ...]


def curate(
    candidates: Sequence[CandidateTag],
    *,
    document_count: int,
    min_cluster_docs: int,
    max_tags: int,
    drop_too_broad_ratio: float = 0.4,
) -> CurationResult:
    merged, notes = merge_duplicates(candidates)
    kept: list[CandidateTag] = []
    for candidate in merged:
        if candidate.coverage < min_cluster_docs:
            notes.append(
                f"dropped {candidate.slug!r}: coverage {candidate.coverage} < {min_cluster_docs}"
            )
            continue
        if document_count >= 10 and candidate.coverage / document_count > drop_too_broad_ratio:
            notes.append(
                f"dropped {candidate.slug!r}: coverage "
                f"{candidate.coverage}/{document_count} too broad"
            )
            continue
        kept.append(candidate)
    kept.sort(key=lambda item: (-item.coverage, item.slug))
    if len(kept) > max_tags:
        notes.extend(
            f"dropped {item.slug!r}: beyond max_tags={max_tags}" for item in kept[max_tags:]
        )
        kept = kept[:max_tags]
    return CurationResult(tuple(kept), tuple(notes))


def render_report(
    *,
    group: str,
    mode: str,
    document_count: int,
    result: CurationResult,
    status: str,
) -> str:
    lines = [
        f"# Tagset bootstrap report: {group}",
        "",
        f"- Status: **{status}**",
        f"- Build mode: `{mode}`",
        f"- Historical articles: {document_count}",
        f"- Curated tags: {len(result.tags)}",
        "",
        "## Tags",
        "",
    ]
    for tag in result.tags:
        lines.extend(
            [
                f"### `{tag.slug}` — {tag.name}",
                "",
                f"- Coverage: {tag.coverage}",
                f"- Description: {tag.description}",
                f"- Keywords: {', '.join(tag.keywords) or '—'}",
                f"- Representatives: {', '.join(tag.representative_titles[:3]) or '—'}",
                "",
            ]
        )
    lines.extend(["## Merged / dropped", ""])
    lines.extend(f"- {note}" for note in result.notes)
    if not result.notes:
        lines.append("- None")
    return "\n".join(lines) + "\n"

"""Frozen, per-group tagset loading and checksum helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.errors import TagsetNotReady
from ..core.models import Tagset, TagsetEntry
from ..core.time import from_iso


def checksum(document: Mapping[str, Any]) -> str:
    """Return the checksum of every field except ``checksum`` itself."""
    payload = {key: value for key, value in document.items() if key != "checksum"}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def freeze_document(document: Mapping[str, Any], *, frozen_at: str | None = None) -> dict[str, Any]:
    """Make a serializable tagset document immutable-by-checksum."""
    frozen = dict(document)
    frozen["frozen_at"] = (
        frozen_at or frozen.get("frozen_at") or datetime.now(UTC).replace(microsecond=0).isoformat()
    )
    frozen.pop("checksum", None)
    frozen["checksum"] = checksum(frozen)
    return frozen


def write_frozen(path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze then atomically replace ``tags.json``."""
    frozen = freeze_document(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(frozen, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return frozen


def _read_document(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TagsetNotReady(f"标签集不存在: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TagsetNotReady(f"标签集无法读取: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise TagsetNotReady(f"标签集根节点必须是对象: {path}")
    return document


def validate_tagset(document: Mapping[str, Any], *, group: str | None = None) -> Tagset:
    """Validate the same frozen document shape used by startup and runtime tagging."""
    expected = document.get("checksum")
    actual = checksum(document)
    if not isinstance(expected, str) or expected != actual:
        raise TagsetNotReady(
            f"标签集 checksum 不匹配: {group or document.get('group') or 'unknown'}"
        )

    actual_group = document.get("group")
    if not isinstance(actual_group, str) or (group is not None and actual_group != group):
        raise TagsetNotReady(f"标签集分组不匹配: 期望 {group!r}，文件为 {actual_group!r}")
    version = document.get("tagset_version")
    build_mode = document.get("build_mode", "llm")
    frozen_at = document.get("frozen_at")
    raw_tags = document.get("tags")
    if not isinstance(version, str) or not version or build_mode not in {"llm", "embedding"}:
        raise TagsetNotReady(f"标签集元数据不完整: {actual_group}")
    parsed_time = from_iso(frozen_at) if isinstance(frozen_at, str) else None
    if parsed_time is None or not isinstance(raw_tags, list):
        raise TagsetNotReady(f"标签集未冻结或 tags 非数组: {actual_group}")

    entries: list[TagsetEntry] = []
    try:
        for raw in raw_tags:
            if not isinstance(raw, dict):
                raise TypeError("tag 不是对象")
            slug, name = raw["slug"], raw["name"]
            description, keywords = raw.get("description", ""), raw.get("keywords", [])
            if not isinstance(slug, str) or not isinstance(name, str):
                raise TypeError("slug/name 不是字符串")
            if not isinstance(description, str) or not isinstance(keywords, list):
                raise TypeError("description/keywords 类型非法")
            centroid = raw.get("centroid")
            entries.append(
                TagsetEntry(
                    slug=slug,
                    name=name,
                    description=description,
                    keywords=tuple(str(item) for item in keywords),
                    threshold=float(raw.get("threshold", 0.35)),
                    centroid=(
                        tuple(float(item) for item in centroid) if centroid is not None else None
                    ),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise TagsetNotReady(f"标签项非法: {actual_group}: {exc}") from exc

    slugs = [entry.slug for entry in entries]
    if any(not slug for slug in slugs) or len(slugs) != len(set(slugs)):
        raise TagsetNotReady(f"标签 slug 为空或重复: {actual_group}")
    if any(not 0 <= entry.threshold <= 1 for entry in entries):
        raise TagsetNotReady(f"标签 threshold 必须在 0..1: {actual_group}")

    return Tagset(
        group_slug=actual_group,
        version=version,
        build_mode=build_mode,
        checksum=expected,
        entries=tuple(entries),
        frozen_at=parsed_time,
    )


def load_tagset(path: Path, *, group: str | None = None) -> Tagset:
    return validate_tagset(_read_document(path), group=group)


def load_groups(root: Path, groups: Iterable[str]) -> dict[str, Tagset]:
    """Load and verify every configured group, naming the broken group in errors."""
    return {group: load_tagset(root / group / "tags.json", group=group) for group in groups}


class TagsetStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._groups: dict[str, Tagset] = {}

    def load(self, groups: Iterable[str]) -> dict[str, Tagset]:
        self._groups = load_groups(self.root, groups)
        return dict(self._groups)

    def get(self, group: str) -> Tagset:
        try:
            return self._groups[group]
        except KeyError as exc:
            raise TagsetNotReady(f"分组 {group!r} 的标签集未加载") from exc

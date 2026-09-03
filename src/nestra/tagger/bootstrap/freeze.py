"""Freeze a reviewed tagset and optionally install it in the existing schema."""

from __future__ import annotations

import json
import os
from array import array
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...core.errors import TagsetNotReady
from ...storage.db import Database
from ..tagset import checksum, freeze_document, validate_tagset, write_frozen


def _pending_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.pending")


def _replace_and_sync(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def recover_pending_tagset(path: Path, db: Database, group: str) -> bool:
    """Finish or discard a freeze interrupted between the DB commit and file replace."""
    pending = _pending_path(path)
    if not pending.exists():
        return False
    try:
        document = json.loads(pending.read_text(encoding="utf-8"))
        tagset = validate_tagset(document, group=group)
    except (OSError, UnicodeError, json.JSONDecodeError, TagsetNotReady):
        pending.unlink(missing_ok=True)
        return False
    row = db.query_one("SELECT status,tagset_version FROM tagset_groups WHERE slug=?", (group,))
    if row and row["status"] == "frozen" and row["tagset_version"] == tagset.version:
        _replace_and_sync(pending, path)
        return True
    pending.unlink(missing_ok=True)
    return False


def freeze_tagset(
    document: Mapping[str, Any], path: Path, *, db: Database | None = None
) -> dict[str, Any]:
    prepared = dict(document)
    if db is None:
        frozen = freeze_document(prepared)
        validate_tagset(frozen, group=str(prepared.get("group", "")))
        return write_frozen(path, frozen)

    group = str(prepared.get("group", ""))
    recover_pending_tagset(path, db, group)

    # IDs are global subscription identities, not per-group ordinals.
    with db.transaction() as conn:
        next_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM tags").fetchone()[0]
        existing = {
            row["slug"]: row["id"]
            for row in conn.execute(
                "SELECT t.id,t.slug FROM tags t JOIN tagset_groups g ON g.id=t.group_id "
                "WHERE g.slug=?",
                (prepared.get("group"),),
            )
        }
        tags = []
        for tag in prepared.get("tags", []):
            tag_id = existing.get(tag.get("slug"))
            if tag_id is None:
                tag_id, next_id = next_id, next_id + 1
            tags.append({**tag, "id": tag_id})
        prepared["tags"] = tags
        frozen = freeze_document(prepared)
        validate_tagset(frozen, group=str(prepared.get("group", "")))
        persist_frozen(db, frozen)
        write_frozen(_pending_path(path), frozen)
    _replace_and_sync(_pending_path(path), path)
    return frozen


def persist_frozen(db: Database, document: Mapping[str, Any]) -> None:
    """Install one frozen version. Runtime tagging never calls this helper."""
    group = document.get("group")
    version = document.get("tagset_version")
    frozen_at = document.get("frozen_at")
    tags = document.get("tags")
    if document.get("checksum") != checksum(document):
        raise TagsetNotReady("拒绝持久化 checksum 无效的标签集")
    if not all(isinstance(value, str) and value for value in (group, version, frozen_at)):
        raise TagsetNotReady("冻结标签集缺少 group/version/frozen_at")
    if not isinstance(tags, list) or any(not isinstance(tag, dict) for tag in tags):
        raise TagsetNotReady("冻结标签集 tags 非数组")

    with db.transaction() as conn:
        row = conn.execute("SELECT id FROM tagset_groups WHERE slug=?", (group,)).fetchone()
        if row is None:
            group_id = conn.execute(
                "INSERT INTO tagset_groups "
                "(slug, name, build_mode, tagset_version, status, frozen_at, created_at) "
                "VALUES (?,?,?,?, 'frozen', ?,?)",
                (
                    group,
                    document.get("group_name", group),
                    document.get("build_mode", "llm"),
                    version,
                    frozen_at,
                    frozen_at,
                ),
            ).lastrowid
        else:
            group_id = row["id"]
            incoming = {str(tag.get("slug", "")) for tag in tags}
            subscribed = {
                item["slug"]
                for item in conn.execute(
                    "SELECT DISTINCT t.slug FROM tags t "
                    "JOIN subscription_tags st ON st.tag_id=t.id WHERE t.group_id=?",
                    (group_id,),
                )
            }
            if removed := subscribed - incoming:
                raise TagsetNotReady(f"拒绝删除仍被订阅的标签: {sorted(removed)}")
            conn.execute(
                "UPDATE tagset_groups SET tagset_version=?, build_mode=?, status='frozen', "
                "frozen_at=? WHERE id=?",
                (version, document.get("build_mode", "llm"), frozen_at, group_id),
            )

        for tag in tags:
            try:
                tag_id = int(tag["id"])
                slug, name = str(tag["slug"]), str(tag["name"])
            except (KeyError, TypeError, ValueError) as exc:
                raise TagsetNotReady(f"冻结标签项缺少 id/slug/name: {tag}") from exc
            existing = conn.execute(
                "SELECT id FROM tags WHERE group_id=? AND slug=?", (group_id, slug)
            ).fetchone()
            if existing:
                tag_id = existing["id"]  # Stable IDs preserve subscriptions across versions.
                conn.execute(
                    "UPDATE tags SET name=?,description=?,keywords=?,threshold=?,"
                    "tagset_version=?,frozen_at=? WHERE id=?",
                    (
                        name,
                        tag.get("description", ""),
                        json.dumps(tag.get("keywords", []), ensure_ascii=False),
                        float(tag.get("threshold", 0.35)),
                        version,
                        frozen_at,
                        tag_id,
                    ),
                )
                conn.execute("DELETE FROM tag_vectors WHERE tag_id=?", (tag_id,))
            else:
                conn.execute(
                    "INSERT INTO tags "
                    "(id, group_id, slug, name, description, keywords, threshold, "
                    "tagset_version, frozen_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        tag_id,
                        group_id,
                        slug,
                        name,
                        tag.get("description", ""),
                        json.dumps(tag.get("keywords", []), ensure_ascii=False),
                        float(tag.get("threshold", 0.35)),
                        version,
                        frozen_at,
                    ),
                )
            if centroid := tag.get("centroid"):
                vector = array("f", (float(value) for value in centroid))
                conn.execute(
                    "INSERT INTO tag_vectors (tag_id, dim, embedding) VALUES (?,?,?)",
                    (tag_id, len(vector), vector.tobytes()),
                )

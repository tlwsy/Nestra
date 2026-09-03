#!/usr/bin/env python3
"""Backfill frozen-tag centroids; fail rather than claim success when local models are absent."""

from __future__ import annotations

import argparse
import json
import sys
from array import array
from pathlib import Path

from nestra.core.config import load_settings
from nestra.storage.db import Database
from nestra.tagger.local_onnx import OnnxEmbeddingRunner
from nestra.tagger.tagset import load_tagset, write_frozen


def backfill(
    db: Database,
    path: Path,
    *,
    group: str,
    model_path: Path,
    tokenizer_path: Path,
) -> int:
    load_tagset(path, group=group)
    if not model_path.is_file() or not tokenizer_path.is_file():
        raise RuntimeError(
            "local embedding model/tokenizer is missing; download both files or pass "
            "--model/--tokenizer. No centroid was written."
        )
    document = json.loads(path.read_text(encoding="utf-8"))
    tags = document.get("tags", [])
    runner = OnnxEmbeddingRunner(model_path, tokenizer_path, idle_unload_sec=0)
    try:
        import numpy as np  # type: ignore[import-not-found]

        vectors: dict[int, object] = {}
        for tag in tags:
            tag_id = int(tag["id"])
            article_ids = [int(item) for item in tag.get("representative_article_ids", [])]
            if article_ids:
                placeholders = ",".join("?" for _ in article_ids)
                rows = db.query(
                    f"SELECT a.title, a.content_text FROM articles a "  # noqa: S608
                    "JOIN sites s ON s.id=a.site_id "
                    "JOIN tagset_groups g ON g.id=s.tagset_group_id "
                    f"WHERE a.id IN ({placeholders}) AND g.slug=? "
                    "AND a.content_text IS NOT NULL",
                    (*article_ids, group),
                )
            else:
                rows = db.query(
                    "SELECT a.title, a.content_text FROM articles a "
                    "JOIN article_tags at ON at.article_id=a.id "
                    "WHERE at.tag_id=? AND a.content_text IS NOT NULL "
                    "ORDER BY at.confidence DESC LIMIT 20",
                    (tag_id,),
                )
            if not rows:
                raise RuntimeError(
                    f"tag {tag['slug']!r} has no representative/tagged articles; "
                    "no files or DB vectors were changed"
                )
            embedded = [
                runner._embed(f"{row['title'] or ''}\n{row['content_text']}") for row in rows
            ]
            centroid = np.asarray(embedded, dtype=np.float32).mean(axis=0)
            centroid /= max(float(np.linalg.norm(centroid)), 1e-12)
            vectors[tag_id] = centroid
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "local embedding dependencies are unavailable; run `uv sync --extra local` "
            "and install numpy. No centroid was written."
        ) from exc
    finally:
        runner._session = runner._tokenizer = None

    for tag in tags:
        tag["centroid"] = [float(value) for value in vectors[int(tag["id"])]]
    document["embedding_model"] = model_path.name
    document["embedding_dim"] = len(next(iter(vectors.values())))
    with db.transaction() as conn:
        for tag in document["tags"]:
            vector = array("f", vectors[int(tag["id"])])
            conn.execute(
                "INSERT INTO tag_vectors(tag_id,dim,embedding) VALUES (?,?,?) "
                "ON CONFLICT(tag_id) DO UPDATE SET dim=excluded.dim, embedding=excluded.embedding",
                (int(tag["id"]), len(vector), vector.tobytes()),
            )
        write_frozen(path, document)
    return len(tags)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--group", required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--tokenizer", type=Path)
    args = parser.parse_args()
    try:
        settings, _ = load_settings(args.config, strict=False)
        db = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
        db.migrate()
        count = backfill(
            db,
            settings.tagset_path(args.group),
            group=args.group,
            model_path=args.model or settings.tagger.local.model_path,
            tokenizer_path=args.tokenizer or settings.tagger.local.tokenizer_path,
        )
    except Exception as exc:
        print(f"centroid backfill failed: {exc}", file=sys.stderr)
        return 1
    print(f"Backfilled and persisted {count} centroids for {args.group}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

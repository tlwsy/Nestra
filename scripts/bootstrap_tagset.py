#!/usr/bin/env python3
"""Build and freeze one tagset group from historical articles in Nestra's database."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

from nestra.core.config import load_settings
from nestra.core.errors import NestraError
from nestra.storage.db import Database
from nestra.tagger.bootstrap import BootstrapOptions, NativeLLMInducer, bootstrap_tagset


async def _run(args: argparse.Namespace) -> int:
    settings, _ = load_settings(args.config, strict=False)
    group = settings.group(args.group)
    if group is None:
        raise ValueError(f"unknown tagset group {args.group!r}")
    db = Database(settings.storage.db_path, cache_mb=settings.runtime.sqlite_cache_mb)
    db.migrate()
    mode = args.mode or group.build_mode or "llm"
    options = BootstrapOptions(
        group=group.slug,
        group_name=group.name,
        mode=mode,
        batch_size=args.batch_size or settings.tagger.tagset.batch_size,
        min_tags=args.min_tags,
        max_tags=args.max_tags or settings.tagger.tagset.auto_curate.max_tags,
        min_cluster_docs=(
            args.min_cluster_docs or settings.tagger.tagset.auto_curate.min_cluster_docs
        ),
        min_documents=group.min_docs_for_build,
        max_documents=args.max_documents,
        require_manual_review=args.require_review or group.require_manual_review,
        reviewed=args.reviewed,
        embedding_model=args.embedding_model,
    )
    timeout = httpx.Timeout(settings.tagger.llm.request_timeout_sec)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        inducer = NativeLLMInducer(settings.tagger.llm.providers, client)
        result = await bootstrap_tagset(
            db,
            args.output or settings.tagger.tagset_dir,
            options,
            inducer=inducer,
        )
    if result.frozen:
        print(f"Frozen {len(result.document['tags'])} tags: {result.tagset_path}")
    else:
        print(
            f"Review required; draft written beside {result.report_path}. No tags were persisted."
        )
    print(f"Report: {result.report_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--group", required=True)
    parser.add_argument("--mode", choices=["llm", "embedding"], default=None)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--min-tags", type=int, default=30)
    parser.add_argument("--max-tags", type=int)
    parser.add_argument("--min-cluster-docs", type=int)
    parser.add_argument("--max-documents", type=int, default=2000)
    parser.add_argument("--require-review", action="store_true")
    parser.add_argument(
        "--reviewed",
        action="store_true",
        help="explicitly confirm this run and pass a configured review gate",
    )
    parser.add_argument("--embedding-model", help="local path or sentence-transformers model name")
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except (NestraError, ValueError) as exc:
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

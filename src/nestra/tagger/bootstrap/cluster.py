"""Optional local embedding/HDBSCAN bootstrap."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from ...core.errors import TagsetNotReady
from .llm_induct import Inducer, invoke_inducer


async def embedding_candidates(
    articles: Sequence[Mapping[str, Any]], options: Any, inducer: Inducer | None
) -> list[Any]:
    if inducer is None:
        raise TagsetNotReady("embedding bootstrap still needs an async inducer to name clusters")
    try:
        import hdbscan  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TagsetNotReady(
            "embedding mode needs optional packages sentence-transformers, hdbscan, and numpy; "
            "install them on the offline build machine, or use --mode llm"
        ) from exc

    model_name = options.embedding_model or "BAAI/bge-small-zh-v1.5"
    try:
        model = SentenceTransformer(model_name)
        texts = [f"{item.get('title', '')}\n{item.get('summary', '')}" for item in articles]
        vectors = np.asarray(model.encode(texts, normalize_embeddings=True), dtype=np.float32)
        labels = hdbscan.HDBSCAN(min_cluster_size=options.min_cluster_docs).fit_predict(vectors)
    except Exception as exc:
        raise TagsetNotReady(
            f"embedding model/clustering unavailable ({model_name}): {exc}. "
            "Provide a reachable model or use --mode llm"
        ) from exc

    candidates = []
    for label in sorted({int(item) for item in labels} - {-1}):
        indexes = np.flatnonzero(labels == label)
        centroid = vectors[indexes].mean(axis=0)
        centroid /= max(float(np.linalg.norm(centroid)), 1e-12)
        nearest = indexes[np.argsort(vectors[indexes] @ centroid)[-3:][::-1]]
        members = [articles[int(index)] for index in indexes]
        prompt = (
            "Name exactly one coherent topic cluster. Return the strict JSON tags structure with "
            "slug, name, decision-boundary description, keywords, article_ids, coverage, and "
            "representative_titles. Do not add other topics. Cluster articles: "
            + repr(
                [
                    {
                        "id": item["id"],
                        "title": item.get("title", ""),
                        "summary": item.get("summary", ""),
                    }
                    for item in members
                ]
            )
        )
        named = await invoke_inducer(inducer, prompt)
        if len(named) != 1:
            raise TagsetNotReady(f"cluster {label} naming returned {len(named)} tags, expected 1")
        candidate = named[0]
        candidates.append(
            replace(
                candidate,
                article_ids=tuple(int(item["id"]) for item in members),
                coverage=len(members),
                representative_titles=tuple(
                    str(articles[int(index)].get("title") or "") for index in nearest
                ),
                centroid=tuple(float(value) for value in centroid),
            )
        )
    if not candidates:
        raise TagsetNotReady(
            "HDBSCAN found no clusters; add historical articles or lower --min-cluster-docs"
        )
    return candidates

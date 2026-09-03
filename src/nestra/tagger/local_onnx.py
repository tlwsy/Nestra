"""Optional lazy ONNX embedding fallback.

Heavy imports occur only on first use. Missing files/dependencies raise locally and the chain keeps
the article EXTRACTED; they never prevent the default LLM-only deployment from starting.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol

from ..core.models import ArticleText, TagAssignment, Tagset
from .base import TagResult


class LocalRunner(Protocol):
    async def tag(self, article: ArticleText, tagset: Tagset) -> TagResult | None: ...


class OnnxEmbeddingRunner:
    def __init__(
        self,
        model_path: Path,
        tokenizer_path: Path,
        *,
        top_k: int = 5,
        threads: int = 1,
        idle_unload_sec: int = 900,
    ) -> None:
        self.model_path, self.tokenizer_path = model_path, tokenizer_path
        self.top_k, self.threads, self.idle_unload_sec = top_k, threads, idle_unload_sec
        self._session: Any = None
        self._tokenizer: Any = None
        self._lock = asyncio.Lock()
        self._unload_task: asyncio.Task[None] | None = None

    @property
    def loaded(self) -> bool:
        return self._session is not None

    def _load(self) -> None:
        if self.loaded:
            return
        if not self.model_path.is_file() or not self.tokenizer_path.is_file():
            raise FileNotFoundError("本地 ONNX 模型或 tokenizer 不存在")
        import onnxruntime as ort  # type: ignore[import-not-found]
        from tokenizers import Tokenizer  # type: ignore[import-not-found]

        options = ort.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(self.model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = Tokenizer.from_file(str(self.tokenizer_path))

    def _embed(self, text: str) -> Any:
        import numpy as np  # type: ignore[import-not-found]

        self._load()
        encoded = self._tokenizer.encode(text[:8000])
        ids = np.asarray([encoded.ids[:512]], dtype=np.int64)
        mask = np.asarray([encoded.attention_mask[:512]], dtype=np.int64)
        types = np.asarray([encoded.type_ids[:512]], dtype=np.int64)
        inputs: dict[str, Any] = {}
        for item in self._session.get_inputs():
            if "mask" in item.name:
                inputs[item.name] = mask
            elif "type" in item.name:
                inputs[item.name] = types
            else:
                inputs[item.name] = ids
        output = self._session.run(None, inputs)[0]
        vector = output[0]
        if vector.ndim == 2:
            weights = mask[0, : vector.shape[0]].astype(np.float32)
            vector = (vector * weights[:, None]).sum(axis=0) / max(float(weights.sum()), 1.0)
        vector = vector.astype(np.float32)
        return vector / max(float(np.linalg.norm(vector)), 1e-12)

    def _tag_sync(self, article: ArticleText, tagset: Tagset) -> TagResult:
        import numpy as np  # type: ignore[import-not-found]

        vector = self._embed(f"{article.title}\n{article.content_text}")
        backend = f"local:{self.model_path.name}"
        scored: list[tuple[float, str]] = []
        for entry in tagset.entries:
            if entry.centroid is None:
                continue
            centroid = np.asarray(entry.centroid, dtype=np.float32)
            if centroid.shape != vector.shape:
                continue
            centroid /= max(float(np.linalg.norm(centroid)), 1e-12)
            score = float(np.dot(vector, centroid))
            if score >= entry.threshold:
                scored.append((min(max(score, 0.0), 1.0), entry.slug))
        assignments = tuple(
            TagAssignment(slug, score, backend)
            for score, slug in sorted(scored, reverse=True)[: self.top_k]
        )
        return TagResult(assignments, backend)

    async def tag(self, article: ArticleText, tagset: Tagset) -> TagResult | None:
        if not tagset.has_centroids:
            return None
        async with self._lock:
            result = await asyncio.to_thread(self._tag_sync, article, tagset)
            if self._unload_task:
                self._unload_task.cancel()
            if self.idle_unload_sec:
                self._unload_task = asyncio.create_task(self._unload_later())
            return result

    async def _unload_later(self) -> None:
        try:
            await asyncio.sleep(self.idle_unload_sec)
            async with self._lock:
                self._session = self._tokenizer = None
        except asyncio.CancelledError:
            pass

    async def aclose(self) -> None:
        if self._unload_task:
            self._unload_task.cancel()
        async with self._lock:
            self._session = self._tokenizer = None


class LocalTagger:
    def __init__(self, *, enabled: bool = False, runner: LocalRunner | None = None) -> None:
        self.enabled = enabled
        self.runner = runner

    async def tag(self, article: ArticleText, tagset: Tagset) -> TagResult | None:
        if not self.enabled or self.runner is None or not tagset.has_centroids:
            return None
        return await self.runner.tag(article, tagset)

    async def aclose(self) -> None:
        close = getattr(self.runner, "aclose", None)
        if close is not None:
            await close()

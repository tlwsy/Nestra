"""Ordered provider/model fallback, persistent health, and result persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..core.config import ProviderConfig, TaggerConfig
from ..core.crypto import Crypto
from ..core.errors import AllBackendsFailed, TaggerError, TagsetNotReady
from ..core.logging import safe_error
from ..core.models import ArticleText, Tagset
from ..core.time import from_iso, now_iso
from ..storage.db import Database
from ..storage.repositories.providers import runtime_providers
from .base import FatalConfigError, OutputInvalidError, QuotaError, TagResult, TransientError
from .local_onnx import LocalRunner, LocalTagger, OnnxEmbeddingRunner
from .providers.anthropic import AnthropicTagger
from .providers.gemini import GeminiTagger
from .providers.openai_compatible import OpenAICompatibleTagger
from .summarizer import summarize

Sleep = Callable[[float], Awaitable[Any]]


class TaggerChain:
    def __init__(
        self,
        config: TaggerConfig,
        db: Database,
        *,
        client: httpx.AsyncClient | None = None,
        local: LocalRunner | None = None,
        crypto: Crypto | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.config = config
        self.db = db
        self.crypto = crypto
        self._client = client or httpx.AsyncClient(
            timeout=config.llm.request_timeout_sec, trust_env=False
        )
        self._owns_client = client is None
        if local is not None:
            self.local = local
        elif config.local.enabled:
            self.local = LocalTagger(
                enabled=True,
                runner=OnnxEmbeddingRunner(
                    config.local.model_path,
                    config.local.tokenizer_path,
                    top_k=config.local.top_k,
                    threads=config.local.intra_op_num_threads,
                    idle_unload_sec=config.local.idle_unload_after_sec,
                ),
            )
        else:
            self.local = LocalTagger()
        self.sleep = sleep

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        close = getattr(self.local, "aclose", None)
        if close is not None:
            await close()

    async def __aenter__(self) -> TaggerChain:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def tag(self, article: ArticleText, tagset: Tagset) -> TagResult:
        strategy = self.config.strategy
        if strategy != "local_only":
            for provider in self._providers():
                if self._cooling_down(provider.name):
                    continue
                skip_provider = False
                for model in provider.models:
                    backend = self._provider(provider, model)
                    transient_attempt = 0
                    output_attempt = 0
                    correction = False
                    while True:
                        self._record_call(provider.name)
                        try:
                            result = await backend.tag(article, tagset, correction=correction)
                        except QuotaError as exc:
                            self._record_failure(provider.name, exc, force_cooldown=True)
                            skip_provider = True
                            break
                        except FatalConfigError as exc:
                            skip_provider = self._record_failure(provider.name, exc)
                            break
                        except OutputInvalidError as exc:
                            skip_provider = self._record_failure(provider.name, exc)
                            if not skip_provider and output_attempt == 0:
                                output_attempt += 1
                                correction = True
                                continue
                            break
                        except TransientError as exc:
                            skip_provider = self._record_failure(provider.name, exc)
                            if (
                                not skip_provider
                                and transient_attempt < self.config.llm.max_retries_per_model
                            ):
                                transient_attempt += 1
                                delay = exc.retry_after_sec
                                if delay is None:
                                    delay = self.config.llm.backoff_base_sec * (
                                        2 ** (transient_attempt - 1)
                                    )
                                if delay:
                                    await self.sleep(delay)
                                continue
                            break
                        else:
                            self._record_success(provider.name)
                            return result
                    if skip_provider:
                        break

        if strategy != "llm_only":
            try:
                result = await self.local.tag(article, tagset)
            except Exception:  # optional backends must degrade, never change article state
                result = None
            if result is not None:
                return result

        raise AllBackendsFailed("所有 LLM provider 和本地兜底均不可用")

    async def tag_article(self, article_id: int, article: ArticleText, tagset: Tagset) -> TagResult:
        """Summarize when enabled, then tag and persist the result."""
        await self.summarize_article(article_id, article)
        result = await self.tag(article, tagset)
        self.persist_result(article_id, tagset, result)
        return result

    async def summarize_article(self, article_id: int, article: ArticleText) -> None:
        setting = self.db.query_one(
            "SELECT s.enabled,s.provider,s.model,a.status,a.summary_backend "
            "FROM ai_summary_settings s JOIN articles a ON a.id=? WHERE s.id=1",
            (article_id,),
        )
        if setting is None:
            raise TaggerError(f"文章 {article_id} 不存在")
        if setting["status"] != "EXTRACTED":
            raise TaggerError(f"文章 {article_id} 状态不是 EXTRACTED: {setting['status']}")
        if not setting["enabled"] or setting["summary_backend"]:
            return

        provider = next(
            (
                item
                for item in self._providers()
                if item.name == setting["provider"] and setting["model"] in item.models
            ),
            None,
        )
        if provider is None:
            raise AllBackendsFailed("配置的总结 provider/model 不可用")
        if self._cooling_down(provider.name):
            raise AllBackendsFailed(f"总结 provider {provider.name} 正在冷却")

        self._record_call(provider.name)
        try:
            summary = await summarize(article, provider, setting["model"], self._client)
        except (
            httpx.HTTPError,
            FatalConfigError,
            OutputInvalidError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            self._record_failure(provider.name, exc)
            raise AllBackendsFailed(
                f"总结后端 {provider.name}/{setting['model']} 失败: {exc}"
            ) from exc
        self._record_success(provider.name)
        self.db.execute(
            "UPDATE articles SET summary=?,summary_backend=?,summarized_at=? "
            "WHERE id=? AND status='EXTRACTED' AND summary_backend IS NULL",
            (summary, f"{provider.name}:{setting['model']}", now_iso(), article_id),
        )

    def persist_result(self, article_id: int, tagset: Tagset, result: TagResult) -> None:
        with self.db.transaction() as conn:
            article = conn.execute(
                "SELECT a.status, g.slug AS group_slug FROM articles a "
                "JOIN sites s ON s.id=a.site_id "
                "JOIN tagset_groups g ON g.id=s.tagset_group_id WHERE a.id=?",
                (article_id,),
            ).fetchone()
            if article is None or article["status"] != "EXTRACTED":
                state = None if article is None else article["status"]
                raise TaggerError(f"文章 {article_id} 状态不是 EXTRACTED: {state}")
            if article["group_slug"] != tagset.group_slug:
                raise TagsetNotReady(
                    f"文章分组 {article['group_slug']!r} 与标签集 {tagset.group_slug!r} 不匹配"
                )

            rows = conn.execute(
                "SELECT t.id, t.slug FROM tags t "
                "JOIN tagset_groups g ON g.id=t.group_id "
                "WHERE g.slug=? AND t.tagset_version=?",
                (tagset.group_slug, tagset.version),
            ).fetchall()
            tag_ids = {row["slug"]: row["id"] for row in rows}
            missing = {item.tag_slug for item in result.assignments} - tag_ids.keys()
            if missing:
                raise TagsetNotReady(f"标签尚未冻结到数据库: {sorted(missing)}")

            conn.execute("DELETE FROM article_tags WHERE article_id=?", (article_id,))
            created_at = now_iso()
            conn.executemany(
                "INSERT INTO article_tags "
                "(article_id, tag_id, confidence, backend, created_at) VALUES (?,?,?,?,?)",
                [
                    (
                        article_id,
                        tag_ids[item.tag_slug],
                        item.confidence,
                        item.backend or result.backend,
                        created_at,
                    )
                    for item in result.assignments
                ],
            )
            conn.execute(
                "UPDATE articles SET status='TAGGED', tagged_at=?, last_error=NULL WHERE id=?",
                (created_at, article_id),
            )

    def _providers(self) -> list[ProviderConfig]:
        if self.crypto is None:
            return list(self.config.llm.providers)
        return runtime_providers(self.config.llm.providers, self.db, self.crypto)

    def _provider(
        self, provider: ProviderConfig, model: str
    ) -> OpenAICompatibleTagger | GeminiTagger | AnthropicTagger:
        backend = {
            "openai_compatible": OpenAICompatibleTagger,
            "gemini": GeminiTagger,
            "anthropic": AnthropicTagger,
        }[provider.type]
        return backend(
            provider,
            model,
            client=self._client,
            top_k=self.config.max_tags_per_article,
            min_confidence=self.config.min_confidence_to_store,
        )

    def _cooling_down(self, provider: str) -> bool:
        row = self.db.query_one(
            "SELECT cooldown_until FROM provider_health WHERE provider=?", (provider,)
        )
        until = from_iso(row["cooldown_until"]) if row else None
        if until is None:
            return False
        if until > datetime.now(UTC):
            return True
        if not self.config.llm.circuit_breaker.half_open_probe:
            self.db.execute(
                "UPDATE provider_health SET consecutive_failures=0, cooldown_until=NULL "
                "WHERE provider=?",
                (provider,),
            )
        return False

    def _record_call(self, provider: str) -> None:
        timestamp = now_iso()
        self.db.execute(
            "INSERT INTO provider_health (provider, total_calls, updated_at) VALUES (?,1,?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "total_calls=total_calls+1, updated_at=excluded.updated_at",
            (provider, timestamp),
        )

    def _record_success(self, provider: str) -> None:
        self.db.execute(
            "UPDATE provider_health SET consecutive_failures=0, cooldown_until=NULL, "
            "last_error=NULL, updated_at=? WHERE provider=?",
            (now_iso(), provider),
        )

    def _record_failure(
        self, provider: str, error: Exception, *, force_cooldown: bool = False
    ) -> bool:
        row = self.db.query_one(
            "SELECT consecutive_failures FROM provider_health WHERE provider=?", (provider,)
        )
        failures = (row["consecutive_failures"] if row else 0) + 1
        should_cool = force_cooldown or (
            failures >= self.config.llm.circuit_breaker.failure_threshold
        )
        cooldown = None
        if should_cool:
            cooldown = (
                (
                    datetime.now(UTC)
                    + timedelta(seconds=self.config.llm.circuit_breaker.cooldown_sec)
                )
                .replace(microsecond=0)
                .isoformat()
            )
        self.db.execute(
            "INSERT INTO provider_health "
            "(provider, consecutive_failures, cooldown_until, last_error, "
            "total_failures, updated_at) "
            "VALUES (?,?,?,?,1,?) ON CONFLICT(provider) DO UPDATE SET "
            "consecutive_failures=excluded.consecutive_failures, "
            "cooldown_until=excluded.cooldown_until, last_error=excluded.last_error, "
            "total_failures=total_failures+1, updated_at=excluded.updated_at",
            (provider, failures, cooldown, safe_error(error), now_iso()),
        )
        return should_cool

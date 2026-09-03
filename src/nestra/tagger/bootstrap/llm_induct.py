"""LLM candidate induction with strict structured-output validation."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from ...core.config import ProviderConfig
from ...core.errors import TagsetNotReady

_SLUG_PARTS = re.compile(r"[^a-z0-9]+")
_NAME_PARTS = re.compile(r"[^\w]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class CandidateTag:
    slug: str
    name: str
    description: str
    keywords: tuple[str, ...] = ()
    article_ids: tuple[int, ...] = ()
    coverage: int = 0
    representative_titles: tuple[str, ...] = ()
    centroid: tuple[float, ...] | None = None


class AsyncInducer(Protocol):
    async def induce(self, prompt: str) -> object: ...


Inducer = AsyncInducer | Callable[[str], Awaitable[object]]


def normalize_slug(value: str) -> str:
    return _SLUG_PARTS.sub("-", value.strip().lower()).strip("-")[:63]


def _name_key(value: str) -> str:
    return _NAME_PARTS.sub("", value.casefold())


def parse_candidates(value: object) -> list[CandidateTag]:
    """Reject anything except a JSON object containing valid candidate tags."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("response is not JSON") from exc
    valid_root = (
        isinstance(value, Mapping)
        and not set(value) - {"tags"}
        and isinstance(value.get("tags"), list)
    )
    if not valid_root:
        raise ValueError('response must be exactly {"tags": [...]}')

    parsed: list[CandidateTag] = []
    for index, raw in enumerate(value["tags"]):
        if not isinstance(raw, Mapping):
            raise ValueError(f"tags[{index}] must be an object")
        slug = normalize_slug(raw.get("slug", "")) if isinstance(raw.get("slug"), str) else ""
        name, description = raw.get("name"), raw.get("description")
        keywords = raw.get("keywords", [])
        article_ids = raw.get("article_ids")
        titles = raw.get("representative_titles", [])
        coverage = raw.get("coverage")
        if not slug or not isinstance(name, str) or not name.strip():
            raise ValueError(f"tags[{index}] has invalid slug/name")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"tags[{index}] has no decision-boundary description")
        if not isinstance(keywords, list) or any(not isinstance(item, str) for item in keywords):
            raise ValueError(f"tags[{index}].keywords must be strings")
        if (
            not isinstance(article_ids, list)
            or not article_ids
            or any(type(item) is not int or item < 1 for item in article_ids)
        ):
            raise ValueError(f"tags[{index}].article_ids must be positive integers")
        if not isinstance(titles, list) or any(not isinstance(item, str) for item in titles):
            raise ValueError(f"tags[{index}].representative_titles must be strings")
        unique_ids = tuple(dict.fromkeys(article_ids))
        if type(coverage) is not int or coverage != len(unique_ids):
            raise ValueError(f"tags[{index}].coverage must equal unique article_ids")
        parsed.append(
            CandidateTag(
                slug,
                name.strip(),
                description.strip(),
                tuple(dict.fromkeys(item.strip() for item in keywords if item.strip())),
                unique_ids,
                coverage,
                tuple(dict.fromkeys(item.strip() for item in titles if item.strip()))[:3],
                None,
            )
        )
    return parsed


def merge_duplicates(candidates: Sequence[CandidateTag]) -> tuple[list[CandidateTag], list[str]]:
    """Normalize and merge exact slug/name duplicates without inventing new tags."""
    merged: list[CandidateTag] = []
    keys: dict[str, int] = {}
    notes: list[str] = []
    for candidate in candidates:
        key_candidates = (f"slug:{candidate.slug}", f"name:{_name_key(candidate.name)}")
        existing = next((keys[key] for key in key_candidates if key in keys), None)
        if existing is None:
            existing = len(merged)
            merged.append(candidate)
        else:
            old = merged[existing]
            ids = tuple(dict.fromkeys((*old.article_ids, *candidate.article_ids)))
            merged[existing] = CandidateTag(
                old.slug,
                old.name,
                max((old.description, candidate.description), key=len),
                tuple(dict.fromkeys((*old.keywords, *candidate.keywords))),
                ids,
                max(old.coverage, candidate.coverage, len(ids)),
                tuple(
                    dict.fromkeys((*old.representative_titles, *candidate.representative_titles))
                )[:3],
                old.centroid or candidate.centroid,
            )
            notes.append(f"merged {candidate.slug!r} into {old.slug!r} (normalized duplicate)")
        for key in key_candidates:
            keys[key] = existing
    return merged, notes


def batch_prompt(articles: Sequence[Mapping[str, Any]], *, max_tags: int) -> str:
    payload = [
        {
            "id": int(article["id"]),
            "title": str(article.get("title") or ""),
            "summary": str(article.get("summary") or "")[:1200],
        }
        for article in articles
    ]
    return (
        "Induce reusable article-topic tags from this batch. Do not classify into an existing "
        "tagset and do not invent facts. Return only one JSON object with key tags. Each tag must "
        "contain slug (ASCII kebab-case), name, a decision-boundary description including "
        "exclusions, "
        "keywords (string array), article_ids (integer array), coverage (integer), and "
        f"representative_titles (up to 3 strings). Return at most {max_tags} tags. Articles:\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def merge_prompt(candidates: Sequence[CandidateTag], *, min_tags: int, max_tags: int) -> str:
    return (
        "Merge synonymous candidates across batches and remove meaningless/navigation/"
        "general-notice topics. Preserve the union of article_ids and never add a topic absent "
        "from the candidates. "
        f"Aim for {min_tags}-{max_tags} tags. Return only the same strict JSON tags structure. "
        "Every description must state inclusion and exclusion boundaries. Candidates:\n"
        + json.dumps(
            [asdict(item) for item in candidates],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


async def invoke_inducer(inducer: Inducer, prompt: str) -> list[CandidateTag]:
    method = getattr(inducer, "induce", None)
    raw = await (method(prompt) if method is not None else inducer(prompt))  # type: ignore[operator]
    if isinstance(raw, list) and all(isinstance(item, CandidateTag) for item in raw):
        return raw
    return parse_candidates(raw)


class NativeLLMInducer:
    """Small native client. Provider order, then model order, is the fallback order."""

    def __init__(self, providers: Sequence[ProviderConfig], client: httpx.AsyncClient) -> None:
        self.providers, self.client = providers, client

    async def induce(self, prompt: str) -> list[CandidateTag]:
        failures: list[str] = []
        for provider in self.providers:
            if not provider.api_key:
                failures.append(f"{provider.name}: missing {provider.api_key_env}")
                continue
            for model in provider.models:
                try:
                    raw = await self._request(provider, model, prompt)
                    return parse_candidates(raw)
                except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                    failures.append(f"{provider.name}/{model}: {exc}")
        detail = "; ".join(failures) or "no providers configured"
        raise TagsetNotReady(f"all bootstrap LLM providers failed: {detail}")

    async def _request(self, provider: ProviderConfig, model: str, prompt: str) -> object:
        if provider.type == "openai_compatible":
            response = await self.client.post(
                f"{provider.base_url.rstrip('/')}/chat/completions",  # type: ignore[union-attr]
                headers={"Authorization": f"Bearer {provider.api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "max_tokens": 8192,
                },
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        if provider.type == "gemini":
            base = (provider.base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip(
                "/"
            )
            response = await self.client.post(
                f"{base}/models/{model}:generateContent",
                headers={"x-goog-api-key": provider.api_key or ""},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0,
                        "responseMimeType": "application/json",
                        "maxOutputTokens": 8192,
                    },
                },
            )
            response.raise_for_status()
            return response.json()["candidates"][0]["content"]["parts"][0]["text"]
        base = (provider.base_url or "https://api.anthropic.com/v1").rstrip("/")
        response = await self.client.post(
            f"{base}/messages",
            headers={
                "x-api-key": provider.api_key or "",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": model,
                "max_tokens": 8192,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        response.raise_for_status()
        return response.json()["content"][0]["text"]

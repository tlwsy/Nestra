"""Small contracts shared by tagger backends and the fallback chain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..core.errors import Fatal, Retryable, TaggerError
from ..core.models import ArticleText, TagAssignment, Tagset


@dataclass(frozen=True, slots=True)
class TagResult:
    assignments: tuple[TagAssignment, ...]
    backend: str


class Tagger(Protocol):
    async def tag(self, article: ArticleText, tagset: Tagset) -> TagResult: ...


class TransientError(TaggerError, Retryable):
    """Network, timeout, rate-limit, or server failure worth retrying."""


class FatalConfigError(TaggerError, Fatal):
    """Credentials, endpoint, or model configuration is permanently invalid."""


class QuotaError(TaggerError, Fatal):
    """Provider quota is exhausted; skip the rest of this provider."""


class OutputInvalidError(TaggerError, Retryable):
    """Provider answered, but its payload cannot be used."""

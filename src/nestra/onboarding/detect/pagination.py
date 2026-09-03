"""Pagination suggestion public API."""

from ..analysis import PaginationCandidate, infer_pagination_direction, suggest_pagination

__all__ = ["PaginationCandidate", "infer_pagination_direction", "suggest_pagination"]

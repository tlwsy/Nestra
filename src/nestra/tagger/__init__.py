"""Frozen-tagset article classification."""

from .base import Tagger, TagResult
from .chain import TaggerChain
from .tagset import TagsetStore, load_tagset

__all__ = ["TagResult", "Tagger", "TaggerChain", "TagsetStore", "load_tagset"]

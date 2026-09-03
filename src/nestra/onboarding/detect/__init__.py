"""Reusable, side-effect-free onboarding detectors."""

from .attachment import AttachmentPatternCandidate, detect_attachment_patterns
from .dualform import DualFormCandidate, confirm_dual_form, dual_form_pairs
from .feed import (
    ResourceCandidate,
    feed_candidates,
    identify_feed,
    identify_sitemap,
    sitemap_candidates,
)
from .listpage import NavigationCandidate, navigation_candidates

__all__ = [
    "AttachmentPatternCandidate",
    "DualFormCandidate",
    "NavigationCandidate",
    "ResourceCandidate",
    "confirm_dual_form",
    "detect_attachment_patterns",
    "dual_form_pairs",
    "feed_candidates",
    "identify_feed",
    "identify_sitemap",
    "navigation_candidates",
    "sitemap_candidates",
]

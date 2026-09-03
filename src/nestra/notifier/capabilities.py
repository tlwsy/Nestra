"""Small channel capability table for Apprise targets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    max_body_chars: int | None
    supports_attachments: bool
    max_attachment_bytes: int | None
    body_formats: frozenset[str]


_FORMATS = frozenset({"text", "markdown", "html"})
_DEFAULT = ChannelCapabilities(None, False, None, _FORMATS)
_CAPABILITIES = {
    "tgram": ChannelCapabilities(4096, True, 50 * 1024 * 1024, _FORMATS),
    "telegram": ChannelCapabilities(4096, True, 50 * 1024 * 1024, _FORMATS),
    "discord": ChannelCapabilities(2000, True, 25 * 1024 * 1024, _FORMATS),
    "slack": ChannelCapabilities(40000, True, 1024 * 1024 * 1024, _FORMATS),
}


def channel_scheme(apprise_url_or_fingerprint: str) -> str:
    """Return only the non-secret scheme from an Apprise URL/fingerprint."""
    return apprise_url_or_fingerprint.partition(":")[0].lower()


def capabilities_for(apprise_url_or_fingerprint: str) -> ChannelCapabilities:
    return _CAPABILITIES.get(channel_scheme(apprise_url_or_fingerprint), _DEFAULT)


def body_limit(apprise_url_or_fingerprint: str, configured_limit: int) -> int:
    channel_limit = capabilities_for(apprise_url_or_fingerprint).max_body_chars
    return min(configured_limit, channel_limit) if channel_limit else configured_limit


def body_format(apprise_url_or_fingerprint: str, requested: str) -> str:
    caps = capabilities_for(apprise_url_or_fingerprint)
    return requested if requested in caps.body_formats else "text"

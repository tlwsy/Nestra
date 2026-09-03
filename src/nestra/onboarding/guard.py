"""Compatibility import for the onboarding README's guard module name."""

from .ssrf import ResolvedUrl, Resolver, resolve_url, system_resolver

__all__ = ["ResolvedUrl", "Resolver", "resolve_url", "system_resolver"]

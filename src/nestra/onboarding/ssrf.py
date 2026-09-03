"""SSRF-safe URL resolution and DNS pinning for onboarding probes."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

from nestra.core.errors import SsrfBlocked

Resolver = Callable[[str, int], Iterable[str]]


@dataclass(frozen=True, slots=True)
class ResolvedUrl:
    """A validated URL rewritten to connect to one already-checked address."""

    url: str
    pinned_url: str
    hostname: str
    host_header: str
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address


def system_resolver(host: str, port: int) -> tuple[str, ...]:
    """Resolve every stream address; callers must validate the whole result set."""
    return tuple(
        dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        )
    )


def _public_ip(value: str, url: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError as exc:
        raise SsrfBlocked(url, f"DNS returned an invalid address: {value!r}") from exc

    # IPv4-mapped IPv6 must inherit the IPv4 classification.
    checked = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    blocked = (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or (checked is not None and not checked.is_global)
    )
    if blocked:
        raise SsrfBlocked(url, f"address is not public: {address}")
    return address


def _parse(url: str) -> tuple[SplitResult, str, int]:
    invalid_character = isinstance(url, str) and any(
        ord(char) <= 32 or ord(char) == 127 for char in url
    )
    if not isinstance(url, str) or not 1 <= len(url) <= 4096 or invalid_character:
        raise SsrfBlocked(
            str(url)[:4096], "URL is empty, too long, or contains control/space characters"
        )
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise SsrfBlocked(url, f"malformed URL: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise SsrfBlocked(url, "only http and https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise SsrfBlocked(url, "credentials in URLs are not allowed")
    if not parsed.hostname:
        raise SsrfBlocked(url, "URL has no hostname")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise SsrfBlocked(url, "invalid hostname") from exc
    if not hostname:
        raise SsrfBlocked(url, "URL has no hostname")
    return parsed, hostname, port or (443 if parsed.scheme.lower() == "https" else 80)


def resolve_url(url: str, resolver: Resolver = system_resolver) -> ResolvedUrl:
    """Validate a URL and pin it to one of its resolved public addresses.

    Every DNS answer is checked before one is selected. The returned URL uses the
    selected IP as the transport origin; ``host_header`` and ``hostname`` preserve
    HTTP Host and TLS SNI/certificate validation respectively.
    """
    parsed, hostname, port = _parse(url)
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
        try:
            raw_addresses = tuple(dict.fromkeys(resolver(hostname, port)))
        except (OSError, UnicodeError) as exc:
            raise SsrfBlocked(url, f"DNS resolution failed: {exc}") from exc
        if not raw_addresses:
            raise SsrfBlocked(url, "DNS resolution returned no addresses") from None
        addresses = tuple(_public_ip(value, url) for value in raw_addresses)
    else:
        addresses = (_public_ip(str(literal), url),)

    selected = addresses[0]
    scheme = parsed.scheme.lower()
    default_port = 443 if scheme == "https" else 80
    ip_host = f"[{selected}]" if selected.version == 6 else str(selected)
    pinned_netloc = ip_host if port == default_port else f"{ip_host}:{port}"
    display_host = f"[{hostname}]" if isinstance(literal, ipaddress.IPv6Address) else hostname
    host_header = display_host if port == default_port else f"{display_host}:{port}"
    clean_url = urlunsplit((scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    pinned_url = urlunsplit((scheme, pinned_netloc, parsed.path or "/", parsed.query, ""))
    return ResolvedUrl(clean_url, pinned_url, hostname, host_header, selected)

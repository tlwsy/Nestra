"""有界、礼貌的异步 HTTP 抓取器。"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..core.config import PolitenessConfig
from ..core.errors import (
    FetchFailed,
    FetchTimeout,
    HttpStatusError,
    NotFound,
    RateLimited,
    ResponseTooLarge,
    RobotsDenied,
    SsrfBlocked,
)
from ..core.models import FetchResult
from ..core.time import backoff_delay, parse_http_date

_MAX_RETRY_AFTER_SEC = 300.0


@dataclass(frozen=True, slots=True)
class BinaryFetchResult:
    final_url: str
    status_code: int
    headers: dict[str, str]
    content: bytes


@dataclass(frozen=True, slots=True)
class CacheValidators:
    etag: str | None = None
    last_modified: str | None = None


class ConditionalCache(Protocol):
    def get(self, url: str) -> CacheValidators | None: ...

    def put(self, url: str, value: CacheValidators) -> None: ...

    def delete(self, url: str) -> None: ...


class MemoryConditionalCache:
    def __init__(self) -> None:
        self._values: dict[str, CacheValidators] = {}

    def get(self, url: str) -> CacheValidators | None:
        return self._values.get(url)

    def put(self, url: str, value: CacheValidators) -> None:
        self._values[url] = value

    def delete(self, url: str) -> None:
        self._values.pop(url, None)


Resolver = Callable[[str], Awaitable[list[str]]]
_CHARSET = re.compile(rb"charset\s*=\s*[\"']?\s*([a-z0-9._-]+)", re.IGNORECASE)


def detect_encoding(headers: httpx.Headers, body: bytes) -> str:
    if body.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if body.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return "utf-32"
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    sample = headers.get("content-type", "").encode("ascii", "ignore") + body[:4096]
    match = _CHARSET.search(sample)
    candidate = match.group(1).decode("ascii").lower() if match else "utf-8"
    if candidate in {"gb2312", "gbk"}:
        candidate = "gb18030"
    try:
        codecs.lookup(candidate)
    except LookupError:
        return "utf-8"
    return candidate


async def _resolve(host: str) -> list[str]:
    records = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(record[4][0] for record in records))


@dataclass(slots=True)
class _RobotsEntry:
    parser: RobotFileParser | None
    expires_at: datetime


class Fetcher:
    """每个实例用于一个站点；实例内共享限速器、robots 与条件缓存。"""

    def __init__(
        self,
        config: PolitenessConfig,
        *,
        max_concurrency: int | None = None,
        delay_sec: float | None = None,
        conditional_requests: bool = True,
        max_bytes: int = 3_145_728,
        cache: ConditionalCache | None = None,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver = _resolve,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.delay_sec = config.delay_sec if delay_sec is None else delay_sec
        self.conditional_requests = conditional_requests
        self.max_bytes = max_bytes
        self.cache = cache or MemoryConditionalCache()
        self._client = client or httpx.AsyncClient(
            timeout=config.timeout_sec,
            headers={"User-Agent": config.user_agent},
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._resolver = resolver
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(max_concurrency or config.max_concurrency)
        self._pace_lock = asyncio.Lock()
        self._last_started = 0.0
        self._robots: dict[str, _RobotsEntry] = {}

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def invalidate_conditional(self, url: str) -> None:
        self.cache.delete(url)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _validate_url(self, url: str) -> str:
        parsed = urlsplit(url)
        has_credentials = parsed.username is not None or parsed.password is not None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or has_credentials:
            raise SsrfBlocked(url, "只允许无凭据的 http/https URL")
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise SsrfBlocked(url, "本地主机")
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                addresses = await self._resolver(host)
            except (OSError, socket.gaierror) as exc:
                raise FetchFailed(f"DNS 解析失败: {host}: {exc}") from exc
        if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise SsrfBlocked(url, "目标解析到非公网地址")
        return addresses[0]

    async def _pace(self) -> None:
        async with self._pace_lock:
            wait = self.delay_sec - (time.monotonic() - self._last_started)
            if wait > 0:
                await self._sleep(wait)
            self._last_started = time.monotonic()

    async def _send(
        self, url: str, headers: dict[str, str], *, max_bytes: int | None = None
    ) -> tuple[httpx.Response, bytes, str]:
        current = url
        limit = self.max_bytes if max_bytes is None else max_bytes
        for _ in range(6):
            address = await self._validate_url(current)
            parsed = urlsplit(current)
            request_url = current
            request_headers = dict(headers)
            extensions = None
            if self._owns_client:
                # Connect to exactly the address that passed SSRF validation. Host/SNI retain
                # the original authority, eliminating the validate-then-resolve-again race.
                ip = ipaddress.ip_address(address)
                authority = f"[{ip}]" if ip.version == 6 else str(ip)
                if parsed.port:
                    authority = f"{authority}:{parsed.port}"
                request_url = urlunsplit(parsed._replace(netloc=authority))
                request_headers["Host"] = parsed.netloc
                extensions = {"sni_hostname": parsed.hostname}
            await self._pace()
            async with self._client.stream(
                "GET", request_url, headers=request_headers, extensions=extensions
            ) as response:
                if response.status_code == 304:
                    return response, b"", current
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise HttpStatusError(f"重定向缺少 Location: {current}")
                    redirected = urljoin(current, location)
                    before, after = urlsplit(current), urlsplit(redirected)
                    if (before.scheme.lower(), before.netloc.lower()) != (
                        after.scheme.lower(),
                        after.netloc.lower(),
                    ):
                        headers = {
                            key: value
                            for key, value in headers.items()
                            if key.lower()
                            not in {"authorization", "cookie", "proxy-authorization", "referer"}
                        }
                    current = redirected
                    continue
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise ResponseTooLarge(f"响应超过 {limit} 字节: {current}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ResponseTooLarge(f"响应超过 {limit} 字节: {current}")
                    chunks.append(chunk)
                return response, b"".join(chunks), current
        raise HttpStatusError(f"重定向次数超过 5: {url}")

    async def _robots_allowed(self, url: str) -> bool:
        if not self.config.respect_robots:
            return True
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        now = datetime.now(UTC)
        cached = self._robots.get(origin)
        if cached and cached.expires_at > now:
            return cached.parser is None or cached.parser.can_fetch(self.config.user_agent, url)

        robots_url = f"{origin}/robots.txt"
        try:
            response, body, _ = await self._send(robots_url, {})
        except (FetchFailed, FetchTimeout, httpx.HTTPError):
            # 网络故障不是「robots 不存在」；保守拒绝，稍后重试业务请求。
            raise FetchFailed(f"无法读取 robots.txt: {robots_url}") from None
        parser: RobotFileParser | None = None
        if response.status_code == 200:
            parser = RobotFileParser(robots_url)
            text = body.decode(detect_encoding(response.headers, body), errors="replace")
            parser.parse(text.splitlines())
        elif response.status_code != 404:
            raise FetchFailed(f"robots.txt 返回 HTTP {response.status_code}: {robots_url}")
        self._robots[origin] = _RobotsEntry(parser, now + timedelta(hours=24))
        return parser is None or parser.can_fetch(self.config.user_agent, url)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if not value:
            return None
        try:
            return min(_MAX_RETRY_AFTER_SEC, max(0.0, float(value)))
        except ValueError:
            try:
                dt = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return min(
                _MAX_RETRY_AFTER_SEC,
                max(0.0, (dt - datetime.now(UTC)).total_seconds()),
            )

    async def fetch_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> BinaryFetchResult:
        """Fetch bounded binary content; retries are handled by the attachment job."""
        if not await self._robots_allowed(url):
            raise RobotsDenied(f"robots.txt 禁止抓取: {url}")
        retry_codes = {x for x in self.config.retry.retry_on if isinstance(x, int)}
        last_error: Exception | None = None
        async with self._semaphore:
            for attempt in range(1, self.config.retry.max_attempts + 1):
                response: httpx.Response | None = None
                try:
                    response, body, final_url = await self._send(
                        url, headers or {}, max_bytes=max_bytes
                    )
                except httpx.TimeoutException as exc:
                    last_error = exc
                    retry = "timeout" in self.config.retry.retry_on
                except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    last_error = exc
                    retry = "connreset" in self.config.retry.retry_on
                else:
                    if response.status_code == 404:
                        raise NotFound(f"HTTP 404: {url}")
                    retry = response.status_code in retry_codes
                    if not retry and response.status_code >= 400:
                        raise HttpStatusError(f"HTTP {response.status_code}: {url}")
                    if not retry:
                        return BinaryFetchResult(
                            final_url, response.status_code, dict(response.headers), body
                        )
                    last_error = (
                        RateLimited(f"HTTP 429: {url}")
                        if response.status_code == 429
                        else FetchFailed(f"HTTP {response.status_code}: {url}")
                    )
                if not retry:
                    break
                if attempt < self.config.retry.max_attempts:
                    requested = self._retry_after(response) if response is not None else None
                    await self._sleep(
                        requested
                        if requested is not None
                        else backoff_delay(
                            attempt,
                            self.config.retry.backoff_base_sec,
                            cap_sec=_MAX_RETRY_AFTER_SEC,
                        )
                    )
        if isinstance(last_error, RateLimited):
            raise RateLimited(
                str(last_error),
                retry_after_sec=self._retry_after(response) if response is not None else None,
            )
        if isinstance(last_error, httpx.TimeoutException):
            raise FetchTimeout(f"请求超时（已重试）: {url}") from last_error
        raise FetchFailed(f"请求失败（已重试）: {url}: {last_error}") from last_error

    async def fetch(self, url: str, *, use_conditional: bool = True) -> FetchResult:
        if not await self._robots_allowed(url):
            raise RobotsDenied(f"robots.txt 禁止抓取: {url}")

        headers: dict[str, str] = {}
        if self.conditional_requests and use_conditional and (cached := self.cache.get(url)):
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified

        retry_codes = {x for x in self.config.retry.retry_on if isinstance(x, int)}
        started = time.monotonic()
        last_error: Exception | None = None
        async with self._semaphore:
            response: httpx.Response | None = None
            for attempt in range(1, self.config.retry.max_attempts + 1):
                response = None
                try:
                    response, body, final_url = await self._send(url, headers)
                except httpx.TimeoutException as exc:
                    last_error = exc
                    retry = "timeout" in self.config.retry.retry_on
                except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    last_error = exc
                    retry = "connreset" in self.config.retry.retry_on
                else:
                    if response.status_code == 304:
                        return FetchResult(
                            url,
                            final_url,
                            304,
                            "",
                            "",
                            elapsed_ms=round((time.monotonic() - started) * 1000),
                            from_cache=True,
                        )
                    if response.status_code == 404:
                        raise NotFound(f"HTTP 404: {url}")
                    retry = response.status_code in retry_codes
                    if not retry and response.status_code >= 400:
                        raise HttpStatusError(f"HTTP {response.status_code}: {url}")
                    if not retry:
                        etag = response.headers.get("etag")
                        modified = response.headers.get("last-modified")
                        if self.conditional_requests and use_conditional and (etag or modified):
                            self.cache.put(url, CacheValidators(etag, modified))
                        encoding = detect_encoding(response.headers, body)
                        return FetchResult(
                            url=url,
                            final_url=final_url,
                            status_code=response.status_code,
                            html=body.decode(encoding, errors="replace"),
                            encoding=encoding,
                            etag=etag,
                            last_modified=parse_http_date(modified) if modified else None,
                            elapsed_ms=round((time.monotonic() - started) * 1000),
                        )
                    if response.status_code == 429:
                        last_error = RateLimited(f"HTTP {response.status_code}: {url}")
                    else:
                        last_error = FetchFailed(f"HTTP {response.status_code}: {url}")

                if attempt < self.config.retry.max_attempts:
                    delay = (
                        self._retry_after(response)
                        if response is not None and response.status_code == 429
                        else None
                    )
                    fallback = backoff_delay(
                        attempt,
                        self.config.retry.backoff_base_sec,
                        cap_sec=_MAX_RETRY_AFTER_SEC,
                    )
                    await self._sleep(delay if delay is not None else fallback)

        if isinstance(last_error, RateLimited):
            retry_after = self._retry_after(response) if response is not None else None
            raise RateLimited(str(last_error), retry_after_sec=retry_after)
        if isinstance(last_error, httpx.TimeoutException):
            raise FetchTimeout(f"请求超时（已重试）: {url}") from last_error
        raise FetchFailed(f"请求失败（已重试）: {url}: {last_error}") from last_error

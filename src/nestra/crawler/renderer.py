"""Opt-in, short-lived Playwright renderer with pinned, bounded proxy fetching."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from ..core.config import PolitenessConfig, RenderConfig
from ..core.errors import CrawlError, FetchTimeout, HttpStatusError
from ..core.models import FetchResult
from .fetcher import Fetcher

_RENDER_LOCK = asyncio.Lock()
_MAX_RESOURCES = 64
_MAX_TOTAL_BYTES = 12 * 1024 * 1024
_RESPONSE_HEADERS = {
    "access-control-allow-origin",
    "cache-control",
    "content-language",
    "content-type",
    "set-cookie",
}
_REQUEST_HEADERS = {"accept", "accept-language", "cookie", "referer"}


class Renderer:
    """Browser network is disabled; every HTTP request is fulfilled by the pinned Fetcher."""

    def __init__(self, politeness: PolitenessConfig, config: RenderConfig | None) -> None:
        self.politeness = politeness
        self.config = config or RenderConfig()

    async def close(self) -> None:
        return None

    async def fetch(self, url: str, *, use_conditional: bool = True) -> FetchResult:
        del use_conditional  # Rendered DOMs do not have reusable conditional response bodies.
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - validated at configuration startup
            raise CrawlError("Playwright 未安装；请使用 render 镜像") from exc

        started = time.monotonic()
        async with (
            _RENDER_LOCK,
            Fetcher(
                self.politeness,
                max_concurrency=1,
                conditional_requests=False,
                max_bytes=3_145_728,
            ) as proxy,
            async_playwright() as playwright,
        ):
            # Even if a route handler is bypassed, Chromium cannot resolve an external host.
            browser = await playwright.chromium.launch(
                args=["--host-resolver-rules=MAP * ~NOTFOUND"]
            )
            final_url = url
            resource_count = 0
            total_bytes = 0
            navigation_error: Exception | None = None
            try:
                context = await browser.new_context(
                    user_agent=self.politeness.user_agent,
                    service_workers="block",
                )
                page = await context.new_page()

                async def proxy_request(route) -> None:
                    nonlocal final_url, navigation_error, resource_count, total_bytes
                    request = route.request
                    target = request.url
                    scheme = urlsplit(target).scheme
                    if scheme in {"about", "blob", "data"}:
                        await route.continue_()
                        return
                    resource_count += 1
                    if request.method != "GET" or resource_count > _MAX_RESOURCES:
                        await route.abort("blockedbyclient")
                        return
                    headers = {
                        key: value
                        for key, value in request.headers.items()
                        if key.lower() in _REQUEST_HEADERS
                    }
                    try:
                        result = await proxy.fetch_bytes(target, headers=headers)
                        total_bytes += len(result.content)
                        if total_bytes > _MAX_TOTAL_BYTES:
                            raise CrawlError("render resource budget exceeded")
                        if request.is_navigation_request() and request.frame == page.main_frame:
                            final_url = result.final_url
                        if result.final_url != target:
                            await route.fulfill(status=302, headers={"location": result.final_url})
                            return
                        await route.fulfill(
                            status=result.status_code,
                            headers={
                                key: value
                                for key, value in result.headers.items()
                                if key.lower() in _RESPONSE_HEADERS
                            },
                            body=result.content,
                        )
                    except Exception as exc:
                        if request.is_navigation_request() and request.frame == page.main_frame:
                            navigation_error = exc
                        await route.abort("blockedbyclient")

                async def block_websocket(websocket) -> None:
                    await websocket.close()

                # Context-level routes also cover popups/new pages created by hostile scripts.
                await context.route_web_socket("**/*", block_websocket)
                await context.route("**/*", proxy_request)
                try:
                    response = await page.goto(
                        url,
                        wait_until=self.config.wait_until,
                        timeout=self.config.timeout_ms,
                    )
                except PlaywrightTimeoutError as exc:
                    if navigation_error:
                        raise navigation_error from exc
                    raise FetchTimeout(f"浏览器加载超时: {url}") from exc
                except Exception as exc:
                    if navigation_error:
                        raise navigation_error from exc
                    raise
                if navigation_error:
                    raise navigation_error
                if response is None:
                    raise CrawlError(f"浏览器未收到主文档响应: {url}")
                if response.status >= 400:
                    raise HttpStatusError(f"HTTP {response.status}: {url}")
                if self.config.wait_selector:
                    await page.wait_for_selector(
                        self.config.wait_selector,
                        timeout=self.config.timeout_ms,
                    )
                html = await page.content()
                if len(html.encode("utf-8")) > 3_145_728:
                    raise CrawlError("rendered DOM exceeds 3145728 bytes")
                return FetchResult(
                    url=url,
                    final_url=final_url,
                    status_code=response.status,
                    html=html,
                    encoding="utf-8",
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
            finally:
                await browser.close()

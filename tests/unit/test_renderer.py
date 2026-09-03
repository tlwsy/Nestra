"""Rendered pages cannot bypass the pinned, bounded fetcher."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from nestra.core.config import PolitenessConfig, RenderConfig
from nestra.core.errors import FetchFailed
from nestra.crawler.fetcher import BinaryFetchResult
from nestra.crawler.renderer import Renderer

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("failure", [False, True])
async def test_renderer_disables_browser_dns_and_proxies_navigation(
    monkeypatch, failure: bool
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    route_scope = {"context": 0, "page": 0}

    class FakeFetcher:
        def __init__(self, *_args, **kwargs):
            assert kwargs["max_bytes"] == 3_145_728

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_bytes(self, url, *, headers):
            calls.append((url, headers))
            if failure:
                raise FetchFailed("temporary disconnect")
            return BinaryFetchResult(
                "https://public.example/final", 200, {"content-type": "text/html"}, b"ok"
            )

    class Route:
        def __init__(self, request):
            self.request = request
            self.fulfilled = None
            self.aborted = False

        async def fulfill(self, **kwargs):
            self.fulfilled = kwargs

        async def abort(self, _reason):
            self.aborted = True

        async def continue_(self):  # pragma: no cover - HTTP must never reach this
            raise AssertionError("browser network bypass")

    class Page:
        main_frame = object()

        async def route(self, pattern, handler):
            route_scope["page"] += 1
            assert pattern == "**/*"
            self.handler = handler

        async def route_web_socket(self, pattern, handler):
            route_scope["page"] += 1
            assert pattern == "**/*"
            websocket = SimpleNamespace(closed=False)

            async def close():
                websocket.closed = True

            websocket.close = close
            await handler(websocket)
            assert websocket.closed

        async def goto(self, url, **_kwargs):
            request = SimpleNamespace(
                url=url,
                method="GET",
                headers={"Authorization": "secret", "Accept": "text/html"},
                frame=self.main_frame,
                is_navigation_request=lambda: True,
            )
            route = Route(request)
            await self.handler(route)
            if route.aborted:
                raise RuntimeError("navigation aborted")
            assert route.fulfilled
            if route.fulfilled["status"] == 302:
                request.url = route.fulfilled["headers"]["location"]
                route = Route(request)
                await self.handler(route)
                assert route.fulfilled and not route.aborted
            return SimpleNamespace(status=route.fulfilled["status"])

        async def content(self):
            return "<html><body>rendered</body></html>"

    class Browser:
        async def new_context(self, **kwargs):
            assert kwargs["service_workers"] == "block"
            return SimpleNamespace(new_page=lambda: None, _kwargs=kwargs)

        async def close(self):
            return None

    page = Page()
    browser = Browser()

    async def new_page():
        return page

    class Context:
        async def new_page(self):
            return await new_page()

        async def route(self, pattern, handler):
            route_scope["context"] += 1
            assert pattern == "**/*"
            page.handler = handler

        async def route_web_socket(self, pattern, handler):
            route_scope["context"] += 1
            assert pattern == "**/*"
            websocket = SimpleNamespace(closed=False)

            async def close():
                websocket.closed = True

            websocket.close = close
            await handler(websocket)
            assert websocket.closed

    async def new_context(**kwargs):
        assert kwargs["service_workers"] == "block"
        return Context()

    browser.new_context = new_context

    class Chromium:
        async def launch(self, *, args):
            assert args == ["--host-resolver-rules=MAP * ~NOTFOUND"]
            return browser

    class PlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            return None

    module = ModuleType("playwright.async_api")
    module.async_playwright = PlaywrightContext
    module.TimeoutError = TimeoutError
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    monkeypatch.setattr("nestra.crawler.renderer.Fetcher", FakeFetcher)

    renderer = Renderer(PolitenessConfig(respect_robots=False, delay_sec=0), RenderConfig())
    if failure:
        with pytest.raises(FetchFailed, match="temporary disconnect"):
            await renderer.fetch("https://public.example/start")
        assert calls == [("https://public.example/start", {"Accept": "text/html"})]
        assert route_scope == {"context": 2, "page": 0}
        return
    result = await renderer.fetch("https://public.example/start")
    assert result.final_url == "https://public.example/final"
    assert calls == [
        ("https://public.example/start", {"Accept": "text/html"}),
        ("https://public.example/final", {"Accept": "text/html"}),
    ]
    assert route_scope == {"context": 2, "page": 0}

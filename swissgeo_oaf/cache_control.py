"""ASGI middleware adding a ``Cache-Control`` header to GET/HEAD responses."""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_CACHEABLE_METHODS = {"GET", "HEAD"}
# Server errors are usually transient and must not be cached.
_MIN_UNCACHEABLE_STATUS = 500


class CacheControlMiddleware:
    """Sets ``Cache-Control`` on GET/HEAD responses that don't already have one.

    5xx responses are skipped so caches don't keep serving a transient server error.
    """

    def __init__(self, app: ASGIApp, value: str) -> None:
        self.app = app
        self.value = value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _CACHEABLE_METHODS or not self.value:
            await self.app(scope, receive, send)
            return

        async def send_with_cache_control(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and message["status"] < _MIN_UNCACHEABLE_STATUS
            ):
                headers = MutableHeaders(scope=message)
                if "cache-control" not in headers:
                    headers["Cache-Control"] = self.value
                # pygeoapi returns different content based on Accept (html vs json) and
                # Accept-Language (en, de, fr, ...) headers
                # NOTE: because we already use the CORSMiddleware, vary header already includes
                # "Origin", see https://github.com/Kludex/starlette/releases/tag/1.7.0
                headers.add_vary_header("Accept, Accept-Language")
            await send(message)

        await self.app(scope, receive, send_with_cache_control)

"""ASGI middleware disabling pygeoapi's HTML output.

pygeoapi picks the output format from the ``f`` query parameter, or else from the
``Accept`` header. An explicit ``f=html`` is rejected with a 400, while ``text/html``
is dropped from ``Accept`` so browsers fall back to JSON instead of getting an error.
"""

import json
from urllib.parse import parse_qsl

from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

_HTML_MIMETYPE = "text/html"


def _strip_html(accept: str) -> str:
    """Return the ``Accept`` header value without its ``text/html`` media ranges."""
    parts = (part.strip() for part in accept.split(","))
    return ",".join(
        part for part in parts if part and part.split(";")[0].strip().lower() != _HTML_MIMETYPE
    )


class DisableHtmlMiddleware:
    """Prevents pygeoapi from rendering HTML responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        query = parse_qsl(scope["query_string"].decode("latin-1"))
        if any(key == "f" and value.strip().lower() == "html" for key, value in query):
            # Same body as pygeoapi's own response to an unsupported format.
            response = Response(
                json.dumps({"code": "InvalidParameterValue", "description": "Invalid format"}),
                status_code=400,
                media_type="application/json",
            )
            await response(scope, receive, send)
            return

        headers = []
        for name, value in scope["headers"]:
            if name != b"accept":
                headers.append((name, value))
            elif accept := _strip_html(value.decode("latin-1")):
                headers.append((name, accept.encode("latin-1")))

        await self.app({**scope, "headers": headers}, receive, send)

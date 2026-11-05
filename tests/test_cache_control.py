"""Tests for the Cache-Control middleware."""

from collections.abc import Iterator

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import pytest

from swissgeo_oaf.cache_control import CacheControlMiddleware

_VALUE = "public, max-age=300"


async def _endpoint(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def _endpoint_with_header(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok", headers={"Cache-Control": "no-store"})


async def _endpoint_with_status(request: Request) -> PlainTextResponse:
    return PlainTextResponse("status", status_code=request.path_params["status"])


@pytest.fixture
def cache_control_value() -> str:
    """Header value passed to the middleware; override with ``@pytest.mark.parametrize``."""
    return _VALUE


@pytest.fixture
def client(cache_control_value: str) -> Iterator[TestClient]:
    """Test client for a minimal app wrapped in the middleware.

    We don't use ``APP`` from ``app.py`` here: importing it monkey-patches pygeoapi's
    ``call_api_threadsafe`` for the whole test session, loads the pygeoapi config and
    starts OTEL; its pygeoapi routes need OpenSearch; and the header value is read once
    from the cached settings at import, so other values can't be tested without reloading
    the module. A minimal app also lets us add a route that sets its own
    ``Cache-Control``, which no pygeoapi GET route does.
    """
    app = Starlette(
        routes=[
            Route("/", _endpoint, methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"]),
            Route("/own", _endpoint_with_header),
            Route("/status/{status:int}", _endpoint_with_status),
        ],
        middleware=[Middleware(CacheControlMiddleware, value=cache_control_value)],
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_sets_header_on_get_and_head(client: TestClient, method: str) -> None:
    response = client.request(method, "/")
    assert response.headers["cache-control"] == _VALUE
    assert response.headers["vary"] == "Accept, Accept-Language"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
def test_no_header_on_other_methods(client: TestClient, method: str) -> None:
    assert "cache-control" not in client.request(method, "/").headers


def test_keeps_existing_header(client: TestClient) -> None:
    assert client.get("/own").headers["cache-control"] == "no-store"


@pytest.mark.parametrize("cache_control_value", [""])
def test_empty_value_disables_header(client: TestClient) -> None:
    assert "cache-control" not in client.get("/").headers


@pytest.mark.parametrize("status", [400, 404, 499])
def test_sets_header_on_4xx(client: TestClient, status: int) -> None:
    response = client.get(f"/status/{status}")
    assert response.status_code == status
    assert response.headers["cache-control"] == _VALUE


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_no_header_on_5xx(client: TestClient, status: int) -> None:
    response = client.get(f"/status/{status}")
    assert response.status_code == status
    assert "cache-control" not in response.headers

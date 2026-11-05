"""Tests for the middleware disabling HTML output."""

from collections.abc import Iterator

from pygeoapi.api import F_HTML, F_JSON, APIRequest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import pytest

from swissgeo_oaf.disable_html import DisableHtmlMiddleware

_BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


async def _endpoint(request: Request) -> JSONResponse:
    # Same format negotiation as the pygeoapi routes.
    return JSONResponse(
        {
            "accept": request.headers.get("accept"),
            "format": APIRequest(request, ["en"]).format,
        }
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Test client for a minimal app wrapped in the middleware.

    See ``tests/test_cache_control.py`` for why ``APP`` from ``app.py`` isn't used.
    """
    app = Starlette(
        routes=[Route("/", _endpoint)],
        middleware=[Middleware(DisableHtmlMiddleware)],
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("value", ["html", "HTML", " html "])
def test_rejects_f_html(client: TestClient, value: str) -> None:
    response = client.get("/", params={"f": value})
    assert response.status_code == 400
    assert response.json() == {"code": "InvalidParameterValue", "description": "Invalid format"}


@pytest.mark.parametrize("value", ["json", "jsonld"])
def test_keeps_other_f_values(client: TestClient, value: str) -> None:
    response = client.get("/", params={"f": value})
    assert response.status_code == 200
    assert response.json()["format"] == value


def test_browser_accept_falls_back_to_json(client: TestClient) -> None:
    body = client.get("/", headers={"Accept": _BROWSER_ACCEPT}).json()
    assert body["accept"] == "application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    assert body["format"] != F_HTML


@pytest.mark.parametrize("accept", ["text/html", "TEXT/HTML;q=0.9", " text/html , "])
def test_removes_html_only_accept(client: TestClient, accept: str) -> None:
    body = client.get("/", headers={"Accept": accept}).json()
    assert body["accept"] is None
    assert body["format"] is None


def test_keeps_json_preference(client: TestClient) -> None:
    body = client.get("/", headers={"Accept": "text/html,application/json;q=0.5"}).json()
    assert body["accept"] == "application/json;q=0.5"
    assert body["format"] == F_JSON

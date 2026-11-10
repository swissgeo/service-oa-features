"""Tests for the syntheticz endpoint."""

from collections.abc import Iterator
from unittest.mock import MagicMock

from pygeoapi.provider.base import ProviderConnectionError
from sqlalchemy.exc import OperationalError
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import pytest

from swissgeo_oaf import syntheticz
from swissgeo_oaf.version import __version__

_PROVIDER_NAME = "swissgeo_oaf.provider.postgresql.SwissGeoProvider"


def _provider_def(table: str) -> dict:
    return {
        "type": "feature",
        "name": _PROVIDER_NAME,
        "data": {"host": "localhost", "dbname": "swissgeo", "search_path": ["public"]},
        "table": table,
        "id_field": "id",
    }


_RESOURCES = {
    # Static, like in pygeoapi-config.yml ...
    "sample-mch-param": {
        "type": "collection",
        "providers": [_provider_def("sample_mch_param")],
    },
    # ... and discovered at startup.
    "ogd-local-forecasting-tre200px": {
        "type": "collection",
        "providers": [_provider_def("tbl_ogd-local-forecasting-tre200px")],
    },
    "other": {
        "type": "collection",
        "providers": [{"type": "feature", "name": "GeoJSON", "data": "/tmp/other.json"}],  # noqa: S108
    },
}


class _FakeProvider:
    """Stands in for SwissGeoProvider, which would connect to the database."""

    def __init__(self, provider_def: dict) -> None:
        self.table = provider_def["table"]
        self.db_search_path = tuple(provider_def["data"]["search_path"])
        self._engine = MagicMock()


@pytest.fixture
def inspector(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock SQLAlchemy inspector; all tables exist by default."""
    inspector = MagicMock()
    inspector.has_table.return_value = True
    monkeypatch.setattr(syntheticz, "SwissGeoProvider", _FakeProvider)
    monkeypatch.setattr(syntheticz, "inspect", lambda _engine: inspector)
    return inspector


@pytest.fixture
def client(inspector: MagicMock) -> Iterator[TestClient]:  # noqa: ARG001
    app = Starlette(routes=[Route("/syntheticz", syntheticz.syntheticz_endpoint(_RESOURCES))])
    with TestClient(app) as test_client:
        yield test_client


def test_syntheticz_up(client: TestClient, inspector: MagicMock) -> None:
    response = client.get("/syntheticz")

    assert response.status_code == 200, (
        f"Unexpected response {response.status_code}: {response.text}"
    )
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "service": {"name": "service-oa-features", "version": __version__},
        "status": "UP",
        "external_systems": {"postgresql": {"status": "UP"}},
    }
    assert [call.args for call in inspector.has_table.call_args_list] == [
        ("sample_mch_param",),
        ("tbl_ogd-local-forecasting-tre200px",),
    ]
    assert {call.kwargs["schema"] for call in inspector.has_table.call_args_list} == {"public"}


@pytest.mark.parametrize(
    "side_effect",
    [
        [True, False],
        OperationalError("SELECT 1", {}, Exception("connection refused")),
        RuntimeError("unexpected"),
    ],
)
def test_syntheticz_down(
    client: TestClient,
    inspector: MagicMock,
    side_effect: object,
) -> None:
    inspector.has_table.side_effect = side_effect

    response = client.get("/syntheticz")

    assert response.status_code == 500, (
        f"Unexpected response {response.status_code}: {response.text}"
    )
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "service": {"name": "service-oa-features", "version": __version__},
        "status": "DOWN",
        "external_systems": {"postgresql": {"status": "DOWN"}},
    }


def test_syntheticz_down_when_provider_fails(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_provider(_provider_def: dict) -> None:
        raise ProviderConnectionError("Could not connect to database")

    monkeypatch.setattr(syntheticz, "SwissGeoProvider", failing_provider)

    response = client.get("/syntheticz")

    assert response.status_code == 500
    assert response.json()["status"] == "DOWN"


def test_no_providers_is_an_error() -> None:
    with pytest.raises(ValueError, match="No PostgreSQL providers found"):
        syntheticz._postgresql_providers({"other": _RESOURCES["other"]})

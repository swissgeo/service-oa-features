"""Tests for the periodic discovery refresh."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import yaml
from pygeoapi.api import API

import pytest

from swissgeo_oaf import discovery, refresh
from swissgeo_oaf.refresh import CollectionRefresher

_PREFIX = "tbl_ogd-local-forecasting-"

_SOURCE = {
    "server": {"url": "http://localhost"},
    "resources": {"static": {"type": "collection", "title": "Static", "providers": []}},
    "swissgeo": {
        "discovery": [
            {
                "tables": f"{_PREFIX}*",
                "collection_id": "{match}",
                "resource": {
                    "type": "collection",
                    "title": "Forecast {match}",
                    "providers": [
                        {
                            "type": "feature",
                            "data": {"host": "${DB_HOST:-localhost}", "search_path": ["public"]},
                        }
                    ],
                },
            }
        ]
    },
}


@pytest.fixture
def tables(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the database: the geometry tables are the returned (mutable) list."""
    tables = [f"{_PREFIX}a"]
    monkeypatch.setattr(discovery, "_connect", lambda _provider: MagicMock())
    monkeypatch.setattr(discovery, "_geometry_tables", lambda _conn, _schema: list(tables))
    monkeypatch.setattr(discovery, "generate_openapi_document", _fake_openapi)
    return tables


def _fake_openapi(path: Path, _fmt: str) -> str:
    """An OpenAPI document listing the generated config's collections as paths."""
    resources = yaml.safe_load(path.read_text())["resources"]
    return yaml.safe_dump({"paths": {f"/collections/{name}": {} for name in resources}})


def _api() -> API:
    """A stand-in for pygeoapi's API object, with the attributes the refresher swaps."""
    resources = {"old": {"title": "Old"}}
    stub = SimpleNamespace(
        config={"server": {"url": "x"}, "resources": resources},
        tpl_config={"server": {"url": "y"}, "resources": dict(resources)},
        openapi={"paths": {}},
    )
    return cast("API", stub)


@pytest.fixture
def refresher(tmp_path: Path, tables: list[str]) -> CollectionRefresher:  # noqa: ARG001
    source = tmp_path / "pygeoapi-config.yml"
    source.write_text(yaml.safe_dump(_SOURCE))
    refresher = CollectionRefresher(source, tmp_path / "openapi.yml", interval_seconds=300)
    refresher.snapshot()
    return refresher


def test_unchanged_tables_do_nothing(refresher: CollectionRefresher) -> None:
    api = _api()
    refresher.attach(api)

    assert refresher.refresh() is False
    assert list(api.config["resources"]) == ["old"]
    assert not refresher.openapi_path.exists()


def test_new_table_is_swapped_in(refresher: CollectionRefresher, tables: list[str]) -> None:
    api = _api()
    refresher.attach(api)
    old_resources = api.config["resources"]
    tables.append(f"{_PREFIX}b")

    assert refresher.refresh() is True

    assert list(api.config["resources"]) == ["static", "a", "b"]
    # ${VAR} references are interpolated, as in pygeoapi's own config.
    assert api.config["resources"]["b"]["providers"][0]["data"]["host"] == "localhost"
    assert api.config["resources"]["b"]["providers"][0]["table"] == f"{_PREFIX}b"
    assert api.tpl_config["resources"] == api.config["resources"]
    assert api.tpl_config["resources"] is not api.config["resources"]
    assert set(api.openapi["paths"]) == {"/collections/static", "/collections/a", "/collections/b"}
    # The rest of the config is left alone, and the old dict is replaced, not mutated.
    assert api.config["server"] == {"url": "x"}
    assert api.tpl_config["server"] == {"url": "y"}
    assert list(old_resources) == ["old"]
    # The generated config keeps the reference.
    generated = discovery.generated_config_path(refresher.openapi_path).read_text()
    assert "${DB_HOST:-localhost}" in generated

    # Same tables on the next tick: nothing to do.
    assert refresher.refresh() is False


def test_removed_table_is_dropped(refresher: CollectionRefresher, tables: list[str]) -> None:
    api = _api()
    refresher.attach(api)
    tables.clear()

    assert refresher.refresh() is True
    assert list(api.config["resources"]) == ["static"]


def test_without_snapshot_first_refresh_rebuilds(tmp_path: Path, tables: list[str]) -> None:
    del tables
    source = tmp_path / "config.yml"
    source.write_text(yaml.safe_dump(_SOURCE))
    refresher = CollectionRefresher(source, tmp_path / "openapi.yml", interval_seconds=300)
    refresher.attach(_api())
    assert refresher.refresh() is True


def test_refresh_before_attach(refresher: CollectionRefresher) -> None:
    with pytest.raises(RuntimeError, match="attach"):
        refresher.refresh()


def test_json_openapi_kept_as_string(tmp_path: Path, tables: list[str]) -> None:
    del tables
    source = tmp_path / "config.yml"
    source.write_text(yaml.safe_dump(_SOURCE))
    refresher = CollectionRefresher(source, tmp_path / "openapi.json", interval_seconds=300)
    api = _api()
    refresher.attach(api)
    refresher.refresh()
    assert isinstance(api.openapi, str)


def test_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYGEOAPI_CONFIG", "/a/config.yml")
    monkeypatch.setenv("PYGEOAPI_OPENAPI", "/b/openapi.yml")
    refresher = CollectionRefresher.from_environment(60)
    assert refresher.source == Path("/a/config.yml")
    assert refresher.openapi_path == Path("/b/openapi.yml")
    assert refresher.enabled
    assert not CollectionRefresher.from_environment(0).enabled


def test_run_keeps_going_after_failure(
    refresher: CollectionRefresher,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = []

    def failing_refresh() -> bool:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database down")
        if len(calls) == 3:
            raise asyncio.CancelledError
        return True

    monkeypatch.setattr(refresher, "refresh", failing_refresh)
    refresher.interval_seconds = 0.001

    async def run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await refresher.run()

    asyncio.run(run())

    assert len(calls) == 3
    assert "discovery refresh failed" in caplog.text
    assert refresh.LOGGER.name == "swissgeo_oaf.refresh"

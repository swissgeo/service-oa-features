"""Tests for the periodic discovery refresh."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import yaml
from babel import Locale
from pygeoapi import l10n
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
                "temporal_extent": True,
                "resource": {
                    "type": "collection",
                    "title": "Forecast {match}",
                    "extents": {
                        "spatial": {"bbox": [5.9, 45.8, 10.5, 47.9]},
                        "temporal": {"begin": None, "end": None, "trs": "gregorian"},
                    },
                    "providers": [
                        {
                            "type": "feature",
                            "data": {"host": "${DB_HOST:-localhost}", "search_path": ["public"]},
                            "time_field": "datetime",
                        }
                    ],
                },
            }
        ]
    },
}


@pytest.fixture(autouse=True)
def _empty_translated_config_cache() -> None:
    """pygeoapi's per-locale cache is process-wide; start every test without it."""
    l10n._cfg_cache.clear()


def _hour(hour: int) -> datetime:
    return datetime(2026, 11, 26, hour, tzinfo=UTC)


@pytest.fixture
def time_ranges(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[datetime, datetime]]:
    """Stub the tables' time ranges: the returned (mutable) dict, by table."""
    time_ranges = {f"{_PREFIX}a": (_hour(0), _hour(10))}
    monkeypatch.setattr(
        discovery,
        "_time_range",
        lambda _conn, _schema, table, _column: time_ranges.get(table),
    )
    return time_ranges


@pytest.fixture
def tables(
    monkeypatch: pytest.MonkeyPatch,
    time_ranges: dict[str, tuple[datetime, datetime]],  # noqa: ARG001
) -> list[str]:
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


def test_swap_invalidates_translated_config_cache(
    refresher: CollectionRefresher, tables: list[str]
) -> None:
    """HTML in a locale rendered before the refresh must see the new collections.

    Regression: pygeoapi caches the translated tpl_config per locale, so without
    the invalidation a new collection's HTML page failed with an UndefinedError.
    """
    api = _api()
    refresher.attach(api)
    english = Locale("en")
    # An HTML page rendered before the refresh caches the old collections.
    assert list(l10n.translate_struct(api.tpl_config, english, is_config=True)["resources"]) == [
        "old"
    ]
    tables.append(f"{_PREFIX}b")

    assert refresher.refresh() is True

    translated = l10n.translate_struct(api.tpl_config, english, is_config=True)
    assert list(translated["resources"]) == ["static", "a", "b"]


def test_unchanged_tables_keep_translated_config_cache(refresher: CollectionRefresher) -> None:
    api = _api()
    refresher.attach(api)
    cached = l10n.translate_struct(api.tpl_config, Locale("en"), is_config=True)

    assert refresher.refresh() is False
    assert l10n.translate_struct(api.tpl_config, Locale("en"), is_config=True) is cached


def _served_api(refresher: CollectionRefresher, tables: list[str]) -> API:
    """An API serving the current discovery, as after startup, via a first full refresh."""
    api = _api()
    refresher.attach(api)
    tables.append(f"{_PREFIX}b")
    assert refresher.refresh() is True
    return api


def test_new_table_gets_its_temporal_extent(
    refresher: CollectionRefresher, tables: list[str]
) -> None:
    api = _served_api(refresher, tables)
    temporal = api.config["resources"]["a"]["extents"]["temporal"]
    assert temporal == {"begin": _hour(0), "end": _hour(10), "trs": "gregorian"}
    # b has no time range yet: the template's extent.
    assert api.config["resources"]["b"]["extents"]["temporal"]["begin"] is None


def test_moved_time_range_swaps_extent_only(
    refresher: CollectionRefresher,
    tables: list[str],
    time_ranges: dict[str, tuple[datetime, datetime]],
) -> None:
    api = _served_api(refresher, tables)
    old_resources = api.config["resources"]
    old_a = old_resources["a"]
    old_b = old_resources["b"]
    english = Locale("en")
    l10n.translate_struct(api.tpl_config, english, is_config=True)
    refresher.openapi_path.unlink()
    time_ranges[f"{_PREFIX}a"] = (_hour(6), _hour(16))

    assert refresher.refresh() is True

    resources = api.config["resources"]
    temporal = resources["a"]["extents"]["temporal"]
    assert temporal == {"begin": _hour(6), "end": _hour(16), "trs": "gregorian"}
    assert resources["a"]["extents"]["spatial"] == old_a["extents"]["spatial"]
    assert resources["b"] is old_b
    # Replaced, not mutated: running requests keep the old extent.
    assert old_resources["a"] is old_a
    assert old_a["extents"]["temporal"]["begin"] == _hour(0)
    assert api.tpl_config["resources"] == resources
    translated = l10n.translate_struct(api.tpl_config, english, is_config=True)
    assert translated["resources"]["a"]["extents"]["temporal"]["begin"] == _hour(6)
    # No regeneration of the config or OpenAPI document.
    assert not refresher.openapi_path.exists()

    assert refresher.refresh() is False


def test_time_range_appearing_rebuilds(
    refresher: CollectionRefresher,
    tables: list[str],
    time_ranges: dict[str, tuple[datetime, datetime]],
) -> None:
    api = _served_api(refresher, tables)
    refresher.openapi_path.unlink()
    time_ranges[f"{_PREFIX}b"] = (_hour(1), _hour(2))

    assert refresher.refresh() is True

    assert api.config["resources"]["b"]["extents"]["temporal"]["begin"] == _hour(1)
    assert refresher.openapi_path.exists()


def test_table_emptied_falls_back_to_template_extent(
    refresher: CollectionRefresher,
    tables: list[str],
    time_ranges: dict[str, tuple[datetime, datetime]],
) -> None:
    api = _served_api(refresher, tables)
    del time_ranges[f"{_PREFIX}a"]

    assert refresher.refresh() is True

    assert api.config["resources"]["a"]["extents"]["temporal"]["begin"] is None


def test_invalidation_tolerates_missing_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(l10n, "_cfg_cache")
    refresh._invalidate_translated_configs()


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

"""Tests for the startup discovery of PostGIS tables."""

import copy
from pathlib import Path
from unittest.mock import MagicMock

import yaml

import pytest

from swissgeo_oaf import discovery
from swissgeo_oaf.discovery import build_resource, discover, match_tables, prepare_config

_PREFIX = "tbl_ogd-local-forecasting-"
_PASSWORD_REFERENCE = "${DB_PW:-swissgeo}"  # noqa: S105 -- an env reference, not a password
_POINT_META = f"{_PREFIX}point-meta"

_TEMPLATE = {
    "type": "collection",
    "title": "Local forecast {match}",
    "description": "From table {table}",
    "providers": [
        {
            "type": "feature",
            "name": "swissgeo_oaf.provider.postgresql.SwissGeoProvider",
            "data": {
                "host": "${DB_HOST:-localhost}",
                "password": _PASSWORD_REFERENCE,
                "search_path": ["public"],
            },
            "id_field": "id",
        }
    ],
}

_RULE = {
    "tables": f"{_PREFIX}*",
    "exclude": [_POINT_META],
    "collection_id": "ogd-local-forecasting-{match}",
    "resource": _TEMPLATE,
}


def _config(rule: dict | None = None, resources: dict | None = None) -> dict:
    """A config with one discovery rule; a deep copy, so tests may mutate it."""
    return copy.deepcopy(
        {
            "server": {"url": "http://localhost"},
            "resources": resources if resources is not None else {},
            "swissgeo": {"discovery": [rule or _RULE]},
        }
    )


# ---------------------------------------------------------------------------
# match_tables
# ---------------------------------------------------------------------------


class TestMatchTables:
    def test_matches_prefix_and_captures_rest(self) -> None:
        tables = [f"{_PREFIX}tre200px", "other_table", f"{_PREFIX}rre150h0"]
        assert match_tables(tables, f"{_PREFIX}*", []) == [
            (f"{_PREFIX}rre150h0", "rre150h0"),
            (f"{_PREFIX}tre200px", "tre200px"),
        ]

    def test_exclude(self) -> None:
        tables = [f"{_PREFIX}tre200px", _POINT_META]
        assert match_tables(tables, f"{_PREFIX}*", [_POINT_META]) == [
            (f"{_PREFIX}tre200px", "tre200px")
        ]

    def test_exclude_is_a_glob(self) -> None:
        tables = [f"{_PREFIX}tre200px", f"{_PREFIX}point-meta", f"{_PREFIX}point-meta-old"]
        assert match_tables(tables, f"{_PREFIX}*", [f"{_PREFIX}point-*"]) == [
            (f"{_PREFIX}tre200px", "tre200px")
        ]

    def test_bare_prefix_does_not_match(self) -> None:
        """`*` must match at least one character."""
        assert match_tables([_PREFIX], f"{_PREFIX}*", []) == []

    def test_regex_characters_are_literal(self) -> None:
        assert match_tables(["tbl.a", "tblxa"], "tbl.*", []) == [("tbl.a", "a")]

    def test_must_match_whole_name(self) -> None:
        assert match_tables(["x_tbl_a", "tbl_a_x"], "tbl_*_x", []) == [("tbl_a_x", "a")]

    def test_pattern_without_star(self) -> None:
        assert match_tables(["exact", "exact2"], "exact", []) == [("exact", "")]

    def test_multiple_stars_capture_first(self) -> None:
        assert match_tables(["a-b-c"], "*-*", []) == [("a-b-c", "a-b")]


# ---------------------------------------------------------------------------
# build_resource
# ---------------------------------------------------------------------------


class TestBuildResource:
    def test_sets_table_and_resource_id_and_formats_texts(self) -> None:
        resource = build_resource(_TEMPLATE, "col", f"{_PREFIX}tre200px", "tre200px")

        assert resource["title"] == "Local forecast tre200px"
        assert resource["description"] == f"From table {_PREFIX}tre200px"
        provider = resource["providers"][0]
        assert provider["table"] == f"{_PREFIX}tre200px"
        assert provider["resource_id"] == "col"

    def test_env_references_untouched(self) -> None:
        resource = build_resource(_TEMPLATE, "col", "t", "m")
        assert resource["providers"][0]["data"]["host"] == "${DB_HOST:-localhost}"

    def test_template_not_mutated(self) -> None:
        build_resource(_TEMPLATE, "col", "t", "m")
        assert "table" not in _TEMPLATE["providers"][0]
        assert _TEMPLATE["title"] == "Local forecast {match}"

    def test_title_override(self) -> None:
        title = {"en": "Air temperature", "de": "Lufttemperatur"}
        resource = build_resource(_TEMPLATE, "col", "t", "m", title)
        assert resource["title"] == title

    def test_language_struct_title_formatted(self) -> None:
        template = {**_TEMPLATE, "title": {"en": "Forecast {match}", "de": "Prognose {match}"}}
        resource = build_resource(template, "col", "t", "x")
        assert resource["title"] == {"en": "Forecast x", "de": "Prognose x"}

    def test_non_string_text_left_alone(self) -> None:
        template = {**_TEMPLATE, "description": None}
        assert build_resource(template, "col", "t", "x")["description"] is None


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub the database: _geometry_tables returns `database.tables`."""
    conn = MagicMock()
    conn.tables = [f"{_PREFIX}tre200px", _POINT_META, "spatial_ref_sys_like"]
    conn.titles = {}
    monkeypatch.setattr(discovery, "_connect", lambda _provider: conn)
    monkeypatch.setattr(discovery, "_geometry_tables", lambda c, _schema: c.tables)
    monkeypatch.setattr(
        discovery, "_lang_title", lambda c, _schema, table, _prefix: c.titles.get(table)
    )
    return conn


class TestDiscover:
    def test_adds_one_resource_per_matching_table(self, database: MagicMock) -> None:
        database.tables.append(f"{_PREFIX}rre150h0")

        result = discover(_config(), _config())

        assert list(result["resources"]) == [
            "ogd-local-forecasting-rre150h0",
            "ogd-local-forecasting-tre200px",
        ]
        resource = result["resources"]["ogd-local-forecasting-tre200px"]
        assert resource["providers"][0]["table"] == f"{_PREFIX}tre200px"
        database.close.assert_called_once()

    def test_builds_from_raw_config(self, database: MagicMock) -> None:  # noqa: ARG002
        """Credentials stay ${VAR} references in the generated resources."""
        interpolated = _config()
        interpolated["swissgeo"]["discovery"][0]["resource"]["providers"][0]["data"] = {
            "host": "db.internal",
            "password": "secret",
        }

        result = discover(_config(), interpolated)

        data = result["resources"]["ogd-local-forecasting-tre200px"]["providers"][0]["data"]
        assert data["password"] == _PASSWORD_REFERENCE

    def test_input_not_mutated(self, database: MagicMock) -> None:  # noqa: ARG002
        raw = _config()
        discover(raw, _config())
        assert raw["resources"] == {}

    def test_keeps_static_resources_and_does_not_override_them(self, database: MagicMock) -> None:
        database.tables.append(f"{_PREFIX}rre150h0")
        static = {"ogd-local-forecasting-tre200px": {"type": "collection", "title": "Static"}}

        result = discover(_config(resources=static), _config(resources=static))

        assert result["resources"]["ogd-local-forecasting-tre200px"]["title"] == "Static"
        assert "ogd-local-forecasting-rre150h0" in result["resources"]

    def test_null_resources(self, database: MagicMock) -> None:  # noqa: ARG002
        raw = _config()
        raw["resources"] = None
        result = discover(raw, _config())
        assert list(result["resources"]) == ["ogd-local-forecasting-tre200px"]

    def test_title_from_columns(self, database: MagicMock) -> None:
        database.titles[f"{_PREFIX}tre200px"] = {"en": "Air temperature"}
        rule = {**_RULE, "title_columns": "parameter_description"}

        result = discover(_config(rule), _config(rule))

        title = result["resources"]["ogd-local-forecasting-tre200px"]["title"]
        assert title == {"en": "Air temperature"}

    def test_template_title_when_no_title_found(self, database: MagicMock) -> None:  # noqa: ARG002
        rule = {**_RULE, "title_columns": "parameter_description"}
        result = discover(_config(rule), _config(rule))
        title = result["resources"]["ogd-local-forecasting-tre200px"]["title"]
        assert title == "Local forecast tre200px"

    def test_collection_id_defaults_to_table(self, database: MagicMock) -> None:  # noqa: ARG002
        rule = {key: value for key, value in _RULE.items() if key != "collection_id"}
        result = discover(_config(rule), _config(rule))
        assert list(result["resources"]) == [f"{_PREFIX}tre200px"]

    def test_no_match_logs_warning(
        self, database: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        database.tables = ["something_else"]
        result = discover(_config(), _config())
        assert result["resources"] == {}
        assert "no table" in caplog.text

    def test_no_discovery_section(self, database: MagicMock) -> None:
        config = {"server": {}, "resources": {"a": {}}}
        assert discover(config, config) == config
        database.close.assert_not_called()

    def test_template_without_feature_provider(self, database: MagicMock) -> None:  # noqa: ARG002
        rule = {**_RULE, "resource": {**_TEMPLATE, "providers": [{"type": "tile"}]}}
        with pytest.raises(ValueError, match="no feature provider"):
            discover(_config(rule), _config(rule))

    def test_connection_closed_on_error(
        self, database: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing(_conn: object, _schema: str) -> list[str]:
            raise RuntimeError("boom")

        monkeypatch.setattr(discovery, "_geometry_tables", failing)
        with pytest.raises(RuntimeError):
            discover(_config(), _config())
        database.close.assert_called_once()


# ---------------------------------------------------------------------------
# database helpers
# ---------------------------------------------------------------------------


def _cursor(conn: MagicMock) -> MagicMock:
    return conn.cursor.return_value.__enter__.return_value


class TestDatabaseHelpers:
    def test_geometry_tables(self) -> None:
        conn = MagicMock()
        _cursor(conn).fetchall.return_value = [("a",), ("b",)]
        assert discovery._geometry_tables(conn, "public") == ["a", "b"]
        assert _cursor(conn).execute.call_args.args[1] == ("public",)

    def test_lang_title(self) -> None:
        conn = MagicMock()
        cursor = _cursor(conn)
        cursor.fetchall.return_value = [
            ("parameter_description_de",),
            ("parameter_description_en",),
            ("value",),
        ]
        cursor.fetchone.return_value = ("Air temperature", "Lufttemperatur")

        title = discovery._lang_title(conn, "public", "t", "parameter_description")

        # Columns are queried in en, de, fr, it order.
        assert title == {"en": "Air temperature", "de": "Lufttemperatur"}

    def test_lang_title_without_columns(self) -> None:
        conn = MagicMock()
        _cursor(conn).fetchall.return_value = [("value",)]
        assert discovery._lang_title(conn, "public", "t", "parameter_description") is None
        _cursor(conn).fetchone.assert_not_called()

    def test_lang_title_empty_table(self) -> None:
        conn = MagicMock()
        _cursor(conn).fetchall.return_value = [("parameter_description_en",)]
        _cursor(conn).fetchone.return_value = None
        assert discovery._lang_title(conn, "public", "t", "parameter_description") is None

    def test_lang_title_skips_empty_values(self) -> None:
        conn = MagicMock()
        _cursor(conn).fetchall.return_value = [("p_en",), ("p_de",)]
        _cursor(conn).fetchone.return_value = ("", None)
        assert discovery._lang_title(conn, "public", "t", "p") is None

    def test_connect_uses_provider_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connect = MagicMock()
        monkeypatch.setattr(discovery.psycopg2, "connect", connect)
        provider = {
            "data": {"host": "h", "port": 1, "dbname": "d", "user": "u", "password": "p"},
            "options": {"sslmode": "require"},
        }

        discovery._connect(provider)

        kwargs = connect.call_args.kwargs
        assert (kwargs["host"], kwargs["port"], kwargs["dbname"]) == ("h", 1, "d")
        assert kwargs["sslmode"] == "require"
        assert kwargs["connect_timeout"] > 0


# ---------------------------------------------------------------------------
# prepare_config
# ---------------------------------------------------------------------------


def test_prepare_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: MagicMock
) -> None:
    del database
    source = tmp_path / "config.yml"
    source.write_text(yaml.safe_dump(_config()))
    openapi = tmp_path / "out" / "openapi.yml"
    openapi.parent.mkdir()
    monkeypatch.setenv("PYGEOAPI_CONFIG", str(source))
    monkeypatch.setenv("PYGEOAPI_OPENAPI", str(openapi))
    monkeypatch.setattr(
        discovery, "generate_openapi_document", lambda path, _fmt: f"openapi: {path.name}\n"
    )

    target = prepare_config()

    assert target == openapi.parent / discovery.GENERATED_CONFIG_NAME
    assert discovery.os.environ["PYGEOAPI_CONFIG"] == str(target)
    generated = yaml.safe_load(target.read_text())
    assert list(generated["resources"]) == ["ogd-local-forecasting-tre200px"]
    # Credentials are written as references, not values.
    data = generated["resources"]["ogd-local-forecasting-tre200px"]["providers"][0]["data"]
    assert data["password"] == _PASSWORD_REFERENCE
    assert openapi.read_text() == f"openapi: {target.name}\n"
    # The source config is left alone.
    assert yaml.safe_load(source.read_text()) == _config()


def test_prepare_config_writes_shared_objects_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: MagicMock
) -> None:
    """YAML anchors in the source config must not come out as &id001 aliases."""
    del database
    source = tmp_path / "config.yml"
    source.write_text(
        "server: {url: http://localhost}\n"
        "swissgeo:\n"
        "  discovery:\n"
        "    - tables: tbl_ogd-local-forecasting-*\n"
        "      exclude: [tbl_ogd-local-forecasting-point-meta]\n"
        "      resource: &template\n"
        "        type: collection\n"
        "        extents: {spatial: {bbox: [5.9, 45.8, 10.5, 47.9]}}\n"
        "        providers: [{type: feature, data: {search_path: [public]}}]\n"
        "resources:\n"
        "  sample:\n"
        "    <<: *template\n"
        "    title: Sample\n"
    )
    monkeypatch.setenv("PYGEOAPI_CONFIG", str(source))
    monkeypatch.setenv("PYGEOAPI_OPENAPI", str(tmp_path / "openapi.yml"))
    monkeypatch.setattr(discovery, "generate_openapi_document", lambda _path, _fmt: "")

    text = prepare_config().read_text()

    assert "&id" not in text
    assert "*id" not in text
    generated = yaml.safe_load(text)
    assert list(generated["resources"]) == ["sample", "tbl_ogd-local-forecasting-tre200px"]
    assert generated["resources"]["sample"]["extents"]["spatial"]["bbox"][0] == 5.9

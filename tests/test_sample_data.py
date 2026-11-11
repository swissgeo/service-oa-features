"""Tests for the sample table seeded on startup."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from swissgeo_oaf import sample_data
from swissgeo_oaf.sample_data import (
    POINT_META_TABLE,
    SAMPLE_TABLE,
    iter_point_meta,
    iter_sample_features,
    seed,
    seed_point_meta,
    seed_sample_data,
)
from swissgeo_oaf.settings import Settings

# The committed subset: 100 points x 10 hourly steps of one parameter.
_SAMPLE_ROWS = 1000
# Rows of the committed ogd-local-forecasting_meta_point.csv.
_POINT_ROWS = 100


class TestIterSampleFeatures:
    def test_reads_the_committed_subset(self) -> None:
        features = list(iter_sample_features())

        assert len(features) == _SAMPLE_ROWS
        assert {feature["parameter_shortname"] for feature in features} == {"tre200h0"}

    def test_feature_has_every_seeded_column(self) -> None:
        feature = next(iter_sample_features())

        # feature_id is computed per row at insert time.
        expected = set(sample_data._SEED_COLUMNS) - {"feature_id"} | {"east", "north"}
        assert expected <= set(feature)

    def test_values(self) -> None:
        feature = next(iter_sample_features())

        assert feature["point_name"] == "Arosa"
        assert feature["point_type_de"] == "Station"
        assert feature["point_type_it"] == "Stazione"
        assert feature["parameter_description_en"].startswith("Air temperature")
        assert feature["parameter_unit"] == "°C"
        assert feature["datetime"] == datetime(2026, 8, 19, 21, tzinfo=UTC)
        # LV95, straight from the export.
        assert (feature["east"], feature["north"]) == (2771031.0, 1184830.0)

    def test_empty_strings_become_null(self) -> None:
        stations = [f for f in iter_sample_features() if f["point_type_id"] == 1]
        assert all(f["postal_code"] is None for f in stations)

    def test_feature_id(self) -> None:
        feature = next(iter_sample_features())
        assert sample_data._feature_id(feature) == "1_1_tre200h0_20260819210000"


def _executed(cur: MagicMock) -> list[str]:
    return [str(call.args[0]) for call in cur.execute.call_args_list]


class TestSeed:
    @pytest.fixture
    def inserted(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
        """Capture the rows execute_values would send."""
        rows: list[tuple] = []

        def fake_execute_values(_cur, _statement, argslist, **_kwargs) -> None:
            rows.extend(argslist)

        monkeypatch.setattr(sample_data.extras, "execute_values", fake_execute_values)
        return rows

    def test_takes_the_advisory_lock_first(self, inserted: list[tuple]) -> None:  # noqa: ARG002
        cur = MagicMock()

        seed(cur)

        assert "pg_advisory_xact_lock" in _executed(cur)[0]

    def test_recreates_table_and_indexes(self, inserted: list[tuple]) -> None:  # noqa: ARG002
        cur = MagicMock()

        seed(cur)

        statements = " ".join(_executed(cur))
        assert "DROP TABLE IF EXISTS" in statements
        assert "CREATE TABLE" in statements
        for column in ("geometry", "datetime", "feature_id"):
            assert f"idx_{SAMPLE_TABLE}_{column}" in statements

    def test_inserts_every_row_with_coordinates_last(self, inserted: list[tuple]) -> None:
        assert seed(MagicMock()) == _SAMPLE_ROWS

        assert len(inserted) == _SAMPLE_ROWS
        first = inserted[0]
        assert len(first) == len(sample_data._SEED_COLUMNS) + 2
        assert first[sample_data._SEED_COLUMNS.index("feature_id")] == (
            "1_1_tre200h0_20260819210000"
        )
        assert first[-2:] == (2771031.0, 1184830.0)


class TestIterPointMeta:
    def test_reads_every_point(self) -> None:
        points = list(iter_point_meta())

        assert len(points) == _POINT_ROWS
        assert {point["point_type_id"] for point in points} == {1, 2, 3}

    def test_has_every_point_column(self) -> None:
        point = next(iter_point_meta())

        assert set(sample_data._POINT_COLUMNS) | {"east", "north"} <= set(point)
        assert (point["point_id"], point["station_abbr"], point["postal_code"]) == (1, "ARO", None)
        assert (point["east"], point["north"]) == (2771031.0, 1184830.0)


class TestSeedPointMeta:
    @pytest.fixture
    def inserted(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
        rows: list[tuple] = []

        def fake_execute_values(_cur, _statement, argslist, **_kwargs) -> None:
            rows.extend(argslist)

        monkeypatch.setattr(sample_data.extras, "execute_values", fake_execute_values)
        return rows

    def test_creates_without_dropping(self, inserted: list[tuple]) -> None:  # noqa: ARG002
        cur = MagicMock()
        cur.fetchone.return_value = (False,)

        seed_point_meta(cur)

        statements = " ".join(_executed(cur))
        assert "DROP" not in statements
        assert "CREATE TABLE IF NOT EXISTS" in statements
        assert "CREATE INDEX IF NOT EXISTS" in statements
        assert f"idx_{POINT_META_TABLE}_geometry" in statements

    def test_fills_an_empty_table(self, inserted: list[tuple]) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = (False,)

        assert seed_point_meta(cur) == _POINT_ROWS
        assert len(inserted) == _POINT_ROWS
        assert len(inserted[0]) == len(sample_data._POINT_COLUMNS) + 2
        assert inserted[0][-2:] == (2771031.0, 1184830.0)

    def test_leaves_a_filled_table_alone(self, inserted: list[tuple]) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = (True,)

        assert seed_point_meta(cur) == 0
        assert inserted == []

    def test_seed_runs_it_under_the_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sample_data.extras, "execute_values", lambda *_a, **_k: None)
        called: list[object] = []
        monkeypatch.setattr(sample_data, "seed_point_meta", called.append)
        cur = MagicMock()

        seed(cur)

        assert called == [cur]


class TestSeedSampleData:
    def test_seeds_in_one_transaction_and_closes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = MagicMock()
        monkeypatch.setattr(sample_data, "_connect", lambda _settings: conn)
        monkeypatch.setattr(sample_data, "seed", lambda _cur: 7)

        assert seed_sample_data(Settings()) == 7

        conn.__enter__.assert_called_once()
        conn.__exit__.assert_called_once()
        conn.close.assert_called_once()

    def test_closes_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = MagicMock()
        monkeypatch.setattr(sample_data, "_connect", lambda _settings: conn)

        def failing(_cur: object) -> int:
            raise RuntimeError("boom")

        monkeypatch.setattr(sample_data, "seed", failing)

        with pytest.raises(RuntimeError):
            seed_sample_data(Settings())
        conn.close.assert_called_once()

    def test_connects_with_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connect = MagicMock()
        monkeypatch.setattr(sample_data.psycopg2, "connect", connect)
        settings = Settings(db_host="db", db_port=6543, db_name="n", db_sslmode="require")

        sample_data._connect(settings)

        kwargs = connect.call_args.kwargs
        assert (kwargs["host"], kwargs["port"], kwargs["dbname"]) == ("db", 6543, "n")
        assert kwargs["sslmode"] == "require"
        assert kwargs["connect_timeout"] == settings.db_connect_timeout

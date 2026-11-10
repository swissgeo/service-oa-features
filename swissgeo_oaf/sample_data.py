"""Create and seed the sample table behind the `sample-mch-param` collection.

Runs when the app starts (see ``app.py``), before the collection discovery and
before pygeoapi loads its config, so the table exists by the time the
collection is served -- in every environment, as the collection is listed in the
pygeoapi-config.yml they all use. Set ``SAMPLE_DATA_ENABLED=false`` to skip it.
``python -m swissgeo_oaf.sample_data`` reseeds without starting the app.

Connects with the app's own DB_* settings, i.e. as the role pygeoapi connects as,
which therefore owns the table. That role owns the database and has been granted
the public schema by ``scripts/init_database.py``, which has to have run first.

The table, `sample_mch_param`, has the columns of the MeteoSwiss OGD
local-forecasting tables (`tbl_ogd-local-forecasting-*`) with geometry in
CH1903+ / LV95 (EPSG:2056), but deliberately not their naming: it is listed
statically under `resources:` in pygeoapi-config.yml, and must not be picked up
by the collection discovery (swissgeo_oaf/discovery.py), which serves the real
tables the forecast pipeline writes.

WARNING: *destructive*. The table is dropped and recreated on every run -- on
every app start, including each uvicorn --reload -- so every row is lost each
time. That is fine for its own rows, which only change with the image, but
nothing else must write to this table.

The drop, recreate and seed run in one transaction, holding an advisory lock:
pods starting together would otherwise race on DROP/CREATE TABLE. A pod waiting
for the lock seeds after the first one commits. Readers -- the pods already
serving -- block on the table lock meanwhile rather than seeing it missing, and a
failed run leaves the previous table in place.

The rows are read from the CSVs in `sample-data/` next to this module (shipped
as package data), a committed subset of the MeteoSwiss OGD local-forecasting
export -- see `scripts/make_sample_data.py` for how to rebuild it.
"""

import csv
import logging
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg2
from psycopg2 import extras, sql
from psycopg2.extensions import connection, cursor

from swissgeo_oaf.settings import Settings, get_settings

logger = logging.getLogger(__name__)

SAMPLE_TABLE = "sample_mch_param"

# Key of the transaction-level advisory lock serialising concurrent runs. Any
# constant works, as long as nothing else in the database uses it.
_ADVISORY_LOCK_KEY = 0x5A3D_E0A1

LV95_SRID = 2056

# Column for column the MeteoSwiss OGD local-forecasting DDL. Worth knowing:
#
# * the per-language labels are flat *_de/_fr/_it/_en columns, exactly as the
#   source ships them; the provider collapses them to the requested language;
# * point_id alone is not unique; the source key is (point_id, point_type_id).
#   station_abbr and postal_code are only populated for some point types, so both
#   are nullable;
# * `geometry` is typed to EPSG:2056 so PostGIS rejects a mismatched geometry at
#   write time rather than storing coordinates that disagree with the column.
_COLUMNS = f"""
  value                     FLOAT8        NOT NULL,
  id                        SERIAL4       NOT NULL,
  datetime                  TIMESTAMPTZ   NOT NULL,
  feature_id                VARCHAR(64)   NOT NULL,
  point_id                  INT4          NOT NULL,
  point_type_id             INT4          NOT NULL,
  point_type_de             VARCHAR(64)   NOT NULL,
  point_type_fr             VARCHAR(64)   NOT NULL,
  point_type_it             VARCHAR(64)   NOT NULL,
  point_type_en             VARCHAR(64)   NOT NULL,
  station_abbr              VARCHAR(64),
  postal_code               INT4,
  point_name                VARCHAR(256),
  point_height_masl         FLOAT8,
  geometry                  GEOMETRY(Geometry, {LV95_SRID}),
  parameter_shortname       VARCHAR(64)   NOT NULL,
  parameter_description_de  VARCHAR(256)  NOT NULL,
  parameter_description_fr  VARCHAR(256)  NOT NULL,
  parameter_description_it  VARCHAR(256)  NOT NULL,
  parameter_description_en  VARCHAR(256)  NOT NULL,
  parameter_group_de        VARCHAR(64)   NOT NULL,
  parameter_group_fr        VARCHAR(64)   NOT NULL,
  parameter_group_it        VARCHAR(64)   NOT NULL,
  parameter_group_en        VARCHAR(64)   NOT NULL,
  parameter_granularity     VARCHAR(64)   NOT NULL,
  parameter_decimals        INT4          NOT NULL,
  parameter_datatype        VARCHAR(64)   NOT NULL,
  parameter_unit            VARCHAR(64),"""


# Columns given a B-tree index, as on the upstream tables. `geometry` gets a GIST
# index besides. datetime is the collection's time_field (OGC API datetime=
# filtering) and feature_id identifies a reading.
_BTREE_COLUMNS = ("datetime", "feature_id")


def _drop_table_sql() -> sql.Composed:
    return sql.SQL("DROP TABLE IF EXISTS {table}").format(table=sql.Identifier(SAMPLE_TABLE))


def _create_table_sql() -> sql.Composed:
    # _COLUMNS is a constant, never user input.
    return sql.SQL(
        "CREATE TABLE {table} (" + _COLUMNS + "\n  CONSTRAINT {pk} PRIMARY KEY (id)\n)"
    ).format(table=sql.Identifier(SAMPLE_TABLE), pk=sql.Identifier(f"pk_{SAMPLE_TABLE}"))


def _create_index_sql() -> list[sql.Composed]:
    table = sql.Identifier(SAMPLE_TABLE)
    statements = [
        sql.SQL("CREATE INDEX {index} ON {table} USING GIST (geometry)").format(
            index=sql.Identifier(f"idx_{SAMPLE_TABLE}_geometry"), table=table
        )
    ]
    statements.extend(
        sql.SQL("CREATE INDEX {index} ON {table} ({column})").format(
            index=sql.Identifier(f"idx_{SAMPLE_TABLE}_{column}"),
            table=table,
            column=sql.Identifier(column),
        )
        for column in _BTREE_COLUMNS
    )
    return statements


# How many rows are handed to the server per INSERT round trip. The committed
# subset is a few thousand rows, which is slow enough one at a time to be worth
# batching but small enough not to need COPY.
SEED_BATCH_SIZE = 1000

# The committed subset of the MeteoSwiss OGD local-forecasting export: three
# semicolon-delimited CSVs that keep the export's own column layout and file
# names. See scripts/make_sample_data.py to rebuild it. UTF-8 here, though the
# upstream export is cp1252.
SAMPLE_DATA_DIR = Path(__file__).resolve().parent / "sample-data"
SAMPLE_DATA_ENCODING = "utf-8"
SAMPLE_DATA_DELIMITER = ";"

POINT_META_CSV = SAMPLE_DATA_DIR / "ogd-local-forecasting_meta_point.csv"
PARAMETER_META_CSV = SAMPLE_DATA_DIR / "ogd-local-forecasting_meta_parameters.csv"

# Every other CSV in the directory is a set of forecast values: one file per (run,
# parameter), with both carried in the file name --
# ``vnut12.lssw.202608200000.tre200h0.csv`` is the tre200h0 values of the run made
# at 2026-08-20T00:00Z. The run time appears nowhere inside the file, and the
# values column is named after the parameter, so the name has to be parsed to read
# the file at all.
FORECAST_FILENAME_RE = re.compile(r"^[^.]+\.[^.]+\.(?P<run>\d{12})\.(?P<parameter>[^.]+)\.csv$")

# Both the run stamp in the file name and the `Date` column are YYYYMMDDHHMM. The
# export carries no offset; MeteoSwiss publishes UTC.
SAMPLE_TIMESTAMP_FORMAT = "%Y%m%d%H%M"

# The languages behind the *_de/_fr/_it/_en column families.
LANGUAGES = ("de", "fr", "it", "en")

# The per-language column families, in the order they are inserted.
PARAMETER_LANG_PREFIXES = ("parameter_description", "parameter_group")
POINT_LANG_PREFIXES = ("point_type",)


def _iter_csv(path: Path) -> Iterator[dict[str, str]]:
    """Stream a sample-data CSV row by row.

    A full (non-subset) run is ~1.2 M rows, so nothing here reads a value file into
    memory whole.
    """
    with path.open(encoding=SAMPLE_DATA_ENCODING, newline="") as handle:
        yield from csv.DictReader(handle, delimiter=SAMPLE_DATA_DELIMITER)


def _parse_timestamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, SAMPLE_TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def _lang_columns(row: dict[str, str], prefixes: tuple[str, ...]) -> dict[str, str]:
    """Copy the `<prefix>_de` / `_fr` / `_it` / `_en` column families through as-is."""
    return {
        f"{prefix}_{language}": row[f"{prefix}_{language}"]
        for prefix in prefixes
        for language in LANGUAGES
    }


def _optional(value: str) -> str | None:
    """Map the export's empty strings to NULL.

    station_abbr, postal_code and parameter_unit are each populated for some rows
    only, and the export leaves the others blank rather than absent.
    """
    return value.strip() or None


def _optional_int(value: str) -> int | None:
    stripped = value.strip()
    return int(stripped) if stripped else None


def _load_parameters() -> dict[str, dict]:
    """Index the parameter metadata by shortname, in table-column shape."""
    return {
        row["parameter_shortname"]: {
            **_lang_columns(row, PARAMETER_LANG_PREFIXES),
            "parameter_granularity": row["parameter_granularity"],
            "parameter_decimals": int(row["parameter_decimals"]),
            "parameter_datatype": row["parameter_datatype"],
            "parameter_unit": _optional(row["parameter_unit"]),
        }
        for row in _iter_csv(PARAMETER_META_CSV)
    }


def _load_points() -> dict[tuple[int, int], dict]:
    """Index the point metadata by (point_id, point_type), the export's real key.

    point_id alone is not unique -- the same id recurs across point types. The
    export also ships a handful of exact duplicate rows; keying them into a dict
    collapses those.

    Only the LV95 coordinates are kept, read straight from the export: the
    tables store EPSG:2056, and nothing here reprojects.
    """
    return {
        (int(row["point_id"]), int(row["point_type_id"])): {
            **_lang_columns(row, POINT_LANG_PREFIXES),
            "point_name": _optional(row["point_name"]),
            "station_abbr": _optional(row["station_abbr"]),
            "postal_code": _optional_int(row["postal_code"]),
            "point_height_masl": float(row["point_height_masl"]),
            "east": float(row["point_coordinates_lv95_east"]),
            "north": float(row["point_coordinates_lv95_north"]),
        }
        for row in _iter_csv(POINT_META_CSV)
    }


def _forecast_files() -> list[tuple[Path, str, datetime]]:
    """Find the value files, reading each one's parameter and run time off its name."""
    files = []
    for path in sorted(SAMPLE_DATA_DIR.glob("*.csv")):
        match = FORECAST_FILENAME_RE.match(path.name)
        if match is None:
            # The two metadata files, which carry no run stamp.
            continue
        files.append((path, match["parameter"], _parse_timestamp(match["run"])))
    if not files:
        raise RuntimeError(f"no forecast value files found in {SAMPLE_DATA_DIR}")
    return files


def iter_sample_features() -> Iterator[dict]:
    """Yield one seed feature per forecast value, joined to its point and parameter.

    What varies within a point is datetime and value -- which is what makes the
    sample data useful for exercising OGC API `datetime=` filtering. The run time
    in the file name is not stored: the table has no column for it.
    """
    parameters = _load_parameters()
    points = _load_points()
    undocumented = 0

    for path, shortname, _run in _forecast_files():
        parameter = parameters.get(shortname)
        if parameter is None:
            raise RuntimeError(
                f"{path.name}: {PARAMETER_META_CSV.name} has no row for parameter {shortname}"
            )

        for row in _iter_csv(path):
            point_id = int(row["point_id"])
            point_type_id = int(row["point_type_id"])
            point = points.get((point_id, point_type_id))
            if point is None:
                # No metadata means no point_type_* labels, which are NOT NULL (and no
                # coordinates). The full export has a few such points; the committed
                # subset has none.
                undocumented += 1
                continue
            yield {
                "parameter_shortname": shortname,
                "value": float(row[shortname]),
                "point_id": point_id,
                "point_type_id": point_type_id,
                "datetime": _parse_timestamp(row["Date"]),
                **parameter,
                **point,
            }

    if undocumented:
        logger.warning(
            "skipped %d forecast value(s) whose point has no metadata",
            undocumented,
        )


def _feature_id(feature: dict) -> str:
    """Compose feature_id from the fields that identify a single reading.

    ``<point_id>_<point_type_id>_<parameter_shortname>_<datetime>``, where the
    timestamp is normalised to UTC and rendered as ``YYYYMMDDHHMMSS`` so the id
    carries none of the ISO-8601 separators::

        1_1_tre200h0_20260819210000

    point_id is not unique on its own in the source data -- the same id recurs
    across point types -- so point_type_id is part of the key rather than
    decoration. The table's primary key is the serial `id`; feature_id is not
    constrained unique.
    """
    compact = feature["datetime"].astimezone(UTC).strftime("%Y%m%d%H%M%S")
    return (
        f"{feature['point_id']}_{feature['point_type_id']}"
        f"_{feature['parameter_shortname']}_{compact}"
    )


def _connect(settings: Settings) -> connection:
    """Connect as the role pygeoapi connects as, with the app's DB_* settings."""
    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_pw,
        sslmode=settings.db_sslmode,
        connect_timeout=settings.db_connect_timeout,
    )


_POINT_COLUMNS = (
    "point_id",
    "point_type_id",
    "point_name",
    "station_abbr",
    "postal_code",
    *(f"{prefix}_{lang}" for prefix in POINT_LANG_PREFIXES for lang in LANGUAGES),
    "point_height_masl",
)

# Insert column order; every name is also a key of the dicts
# iter_sample_features() yields (plus feature_id, computed per row). `id` is
# omitted so the serial assigns it, `geometry` because it is built from east/north.
_SEED_COLUMNS = (
    "value",
    "datetime",
    "feature_id",
    *_POINT_COLUMNS,
    "parameter_shortname",
    *(f"{prefix}_{lang}" for prefix in PARAMETER_LANG_PREFIXES for lang in LANGUAGES),
    "parameter_granularity",
    "parameter_decimals",
    "parameter_datatype",
    "parameter_unit",
)


def _insert(cur: cursor, table: str, columns: tuple[str, ...], features: Iterator[dict]) -> int:
    """Batch-insert `features` into `table`, building `geometry` from east/north.

    The geometry is assembled from bound x/y parameters rather than a per-row SQL
    fragment, so no part of the sample data is ever interpolated into the
    statement text. LV95_SRID is an int constant, never user input.
    """
    statement = sql.SQL("INSERT INTO {table} ({columns}, geometry) VALUES %s").format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
    )
    template = (
        "(" + ", ".join(["%s"] * len(columns)) + f", ST_SetSRID(ST_MakePoint(%s, %s), {LV95_SRID}))"
    )

    # Counted while streaming rather than up front: the row count is a property of
    # the CSVs, and nothing needs them all in memory to find it out.
    inserted = 0

    def rows() -> Iterator[tuple]:
        nonlocal inserted
        for feature in features:
            inserted += 1
            yield (*(feature[column] for column in columns), feature["east"], feature["north"])

    extras.execute_values(cur, statement, rows(), template=template, page_size=SEED_BATCH_SIZE)
    return inserted


def seed(cur: cursor) -> int:
    """Recreate the sample table and insert one row per sample forecast value.

    Must run inside a transaction: the advisory lock is released on commit.
    """
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
    logger.warning("dropping table %s and all its rows", SAMPLE_TABLE)
    cur.execute(_drop_table_sql())
    logger.info("creating table %s (srid %d)", SAMPLE_TABLE, LV95_SRID)
    cur.execute(_create_table_sql())
    for statement in _create_index_sql():
        cur.execute(statement)

    logger.info("seeding %s from %s", SAMPLE_TABLE, SAMPLE_DATA_DIR)

    def features() -> Iterator[dict]:
        for feature in iter_sample_features():
            feature["feature_id"] = _feature_id(feature)
            yield feature

    seeded = _insert(cur, SAMPLE_TABLE, _SEED_COLUMNS, features())
    logger.info("seeded %d sample feature(s) into %s", seeded, SAMPLE_TABLE)
    return seeded


def seed_sample_data(settings: Settings) -> int:
    """Recreate and seed the sample table in one transaction; return the row count."""
    conn = _connect(settings)
    try:
        # `with conn` commits on success and rolls back on error; it does not close.
        with conn, conn.cursor() as cur:
            return seed(cur)
    finally:
        conn.close()


def main() -> int:  # pragma: no cover
    """Reseed the sample table without starting the app."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    seed_sample_data(get_settings())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

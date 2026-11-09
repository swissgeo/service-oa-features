"""Bootstrap the PostGIS database for service-oa-features.

Run as an init container before the app starts (see swissgeo/infra-kubernetes
services/service-oa-features/base/patch-init-database.yaml), and as the
`init-database` service in docker-compose.yml for local development.

The shared `services` RDS instance is only reachable from inside the VPC, so
terraform cannot create the database, its owner role or the PostGIS extension
(see swissgeo/infra-terraform aws/modules/common/aws-rds-postgres/README.md).
Terraform owns the credentials only; this script does the rest with the admin
credentials mounted from `service-oa-features-secrets-db-admin`.

The role, database and PostGIS steps are idempotent and may re-run on every pod
start.

Two tables are created, mirroring the MeteoSwiss OGD local-forecasting table
definitions, both with geometry in CH1903+ / LV95 (EPSG:2056):

* `tbl_ogd-local-forecasting-tre200px`  -- one row per forecast value,
  denormalised with its point and parameter metadata. Backs the
  `sample-mch-param` collection in pygeoapi-config.yml.
* `tbl_ogd-local-forecasting-point-meta` -- one row per forecast point.

The table names contain hyphens, so they are always quoted (sql.Identifier)
rather than interpolated as bare names.

WARNING: the table step is *destructive*. Both tables are dropped and recreated
on every run, so every row is lost each time this script executes -- including
on an ordinary pod restart in a deployed environment.

Work is split across two identities on purpose:

* the admin user (`DB_ADMIN_USER`, an rds_superuser) creates the role, the
  database and the PostGIS extension -- all of which need privileges the owner
  role does not have;
* the owner role (`DB_USER`) drops and recreates the tables and indexes, so that
  the role pygeoapi connects as actually owns the schema objects it reads and
  writes.

Seed data is local-dev only and is skipped unless DB_SEED_SAMPLE_DATA is set to
a truthy value; the sample Swiss features have no business being in a deployed
database. The rows are read from the CSVs in `scripts/sample-data/`, a committed
subset of the MeteoSwiss OGD local-forecasting export -- see that directory's
README for its provenance and `make_sample_data.py` for how to rebuild it.
"""

import csv
import logging
import os
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg2
from psycopg2 import extras, sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT, connection, cursor

logger = logging.getLogger("init_database")

# The RDS master user is a member of rds_superuser, which is required to
# CREATE EXTENSION postgis.
ADMIN_DBNAME = "postgres"

# Both mirror the upstream MeteoSwiss table definitions. The hyphens make these
# identifiers that must always be quoted -- go through sql.Identifier, never an
# f-string.
FORECAST_TABLE = "tbl_ogd-local-forecasting-tre200px"
POINT_META_TABLE = "tbl_ogd-local-forecasting-point-meta"

LV95_SRID = 2056

# Column for column the upstream DDLs. Worth knowing:
#
# * the per-language labels are flat *_de/_fr/_it/_en columns, exactly as the
#   source ships them; the provider collapses them to the requested language;
# * point_id alone is not unique; the source key is (point_id, point_type_id).
#   station_abbr and postal_code are only populated for some point types, so both
#   are nullable;
# * `geometry` is typed to EPSG:2056 so PostGIS rejects a mismatched geometry at
#   write time rather than storing coordinates that disagree with the column.
#
# The upstream DDLs also spell out a named NOT NULL constraint per column. Those
# names are exactly the ones PostgreSQL 18 generates for a column-level NOT NULL
# (<table>_<column>_not_null, truncated to 63 bytes), so they are not repeated.
_FORECAST_COLUMNS = f"""
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

_POINT_META_COLUMNS = f"""
  id                        SERIAL4       NOT NULL,
  point_id                  INT4          NOT NULL,
  point_type_id             INT4          NOT NULL,
  point_name                VARCHAR(256),
  station_abbr              VARCHAR(64),
  postal_code               INT4,
  point_type_de             VARCHAR(64)   NOT NULL,
  point_type_fr             VARCHAR(64)   NOT NULL,
  point_type_it             VARCHAR(64)   NOT NULL,
  point_type_en             VARCHAR(64)   NOT NULL,
  point_height_masl         FLOAT8,
  geometry                  GEOMETRY(Geometry, {LV95_SRID}),"""

# (table, column definitions, columns given a B-tree index). Every table also gets
# a GIST index on `geometry`. On the forecast table, datetime is the collection's
# time_field (OGC API datetime= filtering) and feature_id identifies a reading.
TABLES = (
    (FORECAST_TABLE, _FORECAST_COLUMNS, ("datetime", "feature_id")),
    (POINT_META_TABLE, _POINT_META_COLUMNS, ()),
)


def _drop_table_sql(table: str) -> sql.Composed:
    """Dropped and rebuilt on every run, so the table always matches the definition
    above rather than whatever an earlier version of this script left behind.

    NOTE: this discards all existing rows -- see the data-loss warning in the
    module docstring.
    """
    return sql.SQL("DROP TABLE IF EXISTS {table}").format(table=sql.Identifier(table))


def _create_table_sql(table: str, columns: str) -> sql.Composed:
    # `columns` is one of the constants above, never user input.
    return sql.SQL(
        "CREATE TABLE IF NOT EXISTS {table} (" + columns + "\n  CONSTRAINT {pk} PRIMARY KEY (id)\n)"
    ).format(table=sql.Identifier(table), pk=sql.Identifier(f"pk_{table}"))


def _create_index_sql(table: str, btree_columns: tuple[str, ...]) -> list[sql.Composed]:
    """Index names follow the upstream `idx_<table>_<column>` convention."""
    statements = [
        sql.SQL("CREATE INDEX IF NOT EXISTS {index} ON {table} USING GIST (geometry)").format(
            index=sql.Identifier(f"idx_{table}_geometry"), table=sql.Identifier(table)
        )
    ]
    statements.extend(
        sql.SQL("CREATE INDEX IF NOT EXISTS {index} ON {table} ({column})").format(
            index=sql.Identifier(f"idx_{table}_{column}"),
            table=sql.Identifier(table),
            column=sql.Identifier(column),
        )
        for column in btree_columns
    )
    return statements


# How many rows are handed to the server per INSERT round trip. The committed
# subset is a few thousand rows, which is slow enough one at a time to be worth
# batching but small enough not to need COPY.
SEED_BATCH_SIZE = 1000

# The committed subset of the MeteoSwiss OGD local-forecasting export: three
# semicolon-delimited CSVs that keep the export's own column layout and file
# names. See sample-data/README.md for provenance, make_sample_data.py to rebuild
# it. UTF-8 here, though the upstream export is cp1252.
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


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _connect(dbname: str, user: str | None = None, password: str | None = None) -> connection:
    # RDS runs with rds.force_ssl=1. sslmode=prefer would silently retry without
    # SSL and turn a bad password into a confusing pg_hba error, so default to
    # require here (matches the DB_SSLMODE note in pygeoapi-config.yml).
    conn = psycopg2.connect(
        host=_env("DB_HOST"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=dbname,
        user=user or _env("DB_ADMIN_USER"),
        password=password or _env("DB_ADMIN_PW"),
        sslmode=os.environ.get("DB_SSLMODE", "require"),
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def ensure_role(conn: connection, role: str, password: str) -> None:
    """Create the owner role, or resync its password if it already exists."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone():
            logger.info("role %s already exists, updating password", role)
            cur.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(role)),
                (password,),
            )
        else:
            logger.info("creating role %s", role)
            cur.execute(
                sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(role)),
                (password,),
            )

        # CREATE DATABASE ... OWNER <role> requires the executing user to be a member of <role>
        # ("must be able to SET ROLE"). A real superuser bypasses this, but the RDS master user is
        # only rds_superuser, so it needs an explicit membership grant.
        #
        # This GRANT is unconditional on purpose. pg_has_role() already reports true for a role
        # the current user just created (postgres >= 16 records implicit CREATEROLE ownership),
        # but that implicit membership does NOT satisfy CREATE DATABASE ... OWNER -- so gating
        # the GRANT on it skips the grant exactly when it is needed. Re-granting an existing
        # membership is a no-op.
        cur.execute("SELECT CURRENT_USER")
        current_user = cur.fetchone()[0]
        if current_user != role:
            logger.info("granting role %s to %s", role, current_user)
            cur.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(role), sql.Identifier(current_user))
            )


def ensure_database(conn: connection, dbname: str, owner: str) -> None:
    # CREATE DATABASE cannot run inside a transaction, hence ISOLATION_LEVEL_AUTOCOMMIT
    # in _connect() and the bare (non-context-manager) connection in main().
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        if cur.fetchone():
            logger.info("database %s already exists", dbname)
            return
        logger.info("creating database %s owned by %s", dbname, owner)
        cur.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(dbname), sql.Identifier(owner)
            )
        )


def ensure_postgis(dbname: str, owner: str) -> None:
    """Enable PostGIS. Requires rds_superuser, so it runs as the admin user."""
    # The extension is per-database, so this needs a second connection to the
    # service database rather than the admin one.
    conn = _connect(dbname)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
            row = cur.fetchone()
            logger.info("postgis extension available (version %s)", row[0] if row else "unknown")

            # Let the owner role create the schema objects in ensure_schema().
            cur.execute(sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(sql.Identifier(owner)))
    finally:
        conn.close()


def ensure_schema(dbname: str, owner: str, owner_password: str, seed: bool) -> None:
    """Recreate the tables and indexes, and optionally seed sample data.

    Each table is dropped first, so any existing rows are discarded.

    Connects as the owner role rather than the admin user so that the objects are
    owned by the role pygeoapi actually connects as -- otherwise every table would
    belong to the RDS master user and the service would need extra grants.
    """
    conn = _connect(dbname, user=owner, password=owner_password)
    try:
        with conn.cursor() as cur:
            for table, columns, btree_columns in TABLES:
                logger.warning("dropping table %s and all its rows", table)
                cur.execute(_drop_table_sql(table))
                logger.info("creating table %s (srid %d)", table, LV95_SRID)
                cur.execute(_create_table_sql(table, columns))
                for statement in _create_index_sql(table, btree_columns):
                    cur.execute(statement)

            if seed:
                _seed_point_meta(cur)
                _seed_forecast(cur)
            else:
                logger.info("skipping sample data (set DB_SEED_SAMPLE_DATA=true to enable)")
    finally:
        conn.close()


_POINT_COLUMNS = (
    "point_id",
    "point_type_id",
    "point_name",
    "station_abbr",
    "postal_code",
    *(f"{prefix}_{lang}" for prefix in POINT_LANG_PREFIXES for lang in LANGUAGES),
    "point_height_masl",
)

# Insert column order of the forecast table; every name is also a key of the dicts
# iter_sample_features() yields (plus feature_id, computed per row). `id` is
# omitted so the serial assigns it, `geometry` because it is built from east/north.
_FORECAST_SEED_COLUMNS = (
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


def _seed_point_meta(cur: cursor) -> None:
    """Insert one row per point of the local-dev sample data."""
    logger.info("seeding %s from %s", POINT_META_TABLE, POINT_META_CSV)
    points = (
        {"point_id": point_id, "point_type_id": point_type_id, **point}
        for (point_id, point_type_id), point in _load_points().items()
    )
    seeded = _insert(cur, POINT_META_TABLE, _POINT_COLUMNS, points)
    logger.info("seeded %d point(s) into %s", seeded, POINT_META_TABLE)


def _seed_forecast(cur: cursor) -> None:
    """Insert one row per forecast value of the local-dev sample data."""
    logger.info("seeding %s from %s", FORECAST_TABLE, SAMPLE_DATA_DIR)

    def features() -> Iterator[dict]:
        for feature in iter_sample_features():
            feature["feature_id"] = _feature_id(feature)
            yield feature

    seeded = _insert(cur, FORECAST_TABLE, _FORECAST_SEED_COLUMNS, features())
    logger.info("seeded %d sample feature(s) into %s", seeded, FORECAST_TABLE)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    dbname = _env("DB_NAME")
    owner = _env("DB_USER")
    owner_password = _env("DB_PW")
    seed = _env_flag("DB_SEED_SAMPLE_DATA")

    # Note: no `with conn` here. psycopg2 connection context managers wrap the
    # body in a transaction, which defeats the autocommit that CREATE DATABASE
    # requires.
    conn = _connect(ADMIN_DBNAME)
    try:
        ensure_role(conn, owner, owner_password)
        ensure_database(conn, dbname, owner)
    finally:
        conn.close()

    ensure_postgis(dbname, owner)
    ensure_schema(dbname, owner, owner_password, seed)

    logger.info("database bootstrap complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

Two feature tables are created, holding the same readings in different
coordinate reference systems:

* `sample_features`      -- geometry in WGS84 (EPSG:4326)
* `sample_features_lv95` -- geometry in CH1903+ / LV95 (EPSG:2056)

Each backs one collection in pygeoapi-config.yml, and each collection declares
its table's SRID as its `storage_crs`, so neither read path reprojects. Both
coordinate pairs are published by the source export, so the LV95 rows are
MeteoSwiss's own easting/northing rather than a transform of the degrees.

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
import json
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

# The WGS84 table and the LV95 table hold the same readings; they differ only in
# the SRID of `geom` and in which pair of source columns it is built from. Each
# backs one collection in pygeoapi-config.yml, so neither read path reprojects.
TABLE_NAME = "sample_features"
TABLE_NAME_LV95 = "sample_features_lv95"

WGS84_SRID = 4326
LV95_SRID = 2056

# (table, srid, feature keys holding the x/y for that srid). The key pairs index
# into the dicts iter_sample_features() yields.
TABLES = (
  (TABLE_NAME, WGS84_SRID, ("lon", "lat")),
  (TABLE_NAME_LV95, LV95_SRID, ("east", "north")),
)


def _drop_table_sql(table: str) -> str:
  """Dropped and rebuilt on every run, so the table always matches the definition
  below rather than whatever an earlier version of this script left behind.

  NOTE: this discards all existing rows -- see the data-loss warning in the
  module docstring.
  """
  return f"DROP TABLE IF EXISTS {table}"


def _create_table_sql(table: str, srid: int) -> str:
  """parameter_description, parameter_group and point_type_name are JSONB language
  structs ({"de": …, "fr": …}); the provider collapses them to the requested
  language via pygeoapi's l10n. Everything else is a plain scalar.

  The columns mirror the MeteoSwiss OGD local-forecasting CSVs the sample data is
  read from (see SAMPLE_DATA_DIR). Two consequences worth knowing:

  * point_name is TEXT, not a language struct. The source ships a single name per
    point ("Arosa", "Delémont"), and it is point_type that carries the localised
    labels -- hence the separate point_type_name column.
  * point_id alone is not unique; the source key is (point_id, point_type), which
    is why external_id is built from both. station_abbr and postal_code are only
    populated for some point types, so both stay nullable.

  `geom` is typed to `srid` so PostGIS rejects a mismatched geometry at write
  time rather than storing coordinates that silently disagree with the column's
  declared CRS.
  """
  return f"""
CREATE TABLE IF NOT EXISTS {table} (
  external_id           TEXT PRIMARY KEY,
  parameter_shortname   TEXT,
  parameter_description JSONB,
  parameter_group       JSONB,
  parameter_unit        TEXT,
  value                 FLOAT,
  point_id              INT,
  point_type            INT,
  point_name            TEXT,
  point_type_name       JSONB,
  station_abbr          TEXT,
  postal_code           TEXT,
  point_height_masl     FLOAT,
  forecast_datetime     TIMESTAMPTZ NOT NULL,
  created               TIMESTAMPTZ NOT NULL DEFAULT now(),
  geom                  GEOMETRY(Geometry, {srid}) NOT NULL
)
"""


def _create_index_sql(table: str) -> tuple[str, ...]:
  return (
    f"CREATE INDEX IF NOT EXISTS {table}_geom_idx ON {table} USING GIST (geom)",
    f"CREATE INDEX IF NOT EXISTS {table}_created_idx ON {table} (created)",
    # forecast_datetime is the collection's time_field, so this is the index that
    # backs OGC API datetime= filtering.
    f"CREATE INDEX IF NOT EXISTS {table}_forecast_datetime_idx ON {table} (forecast_datetime)",
  )


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

# The languages behind the *_de/_fr/_it/_en column families, and so the keys of
# the JSONB language structs built from them.
LANGUAGES = ("de", "fr", "it", "en")


def _iter_csv(path: Path) -> Iterator[dict[str, str]]:
  """Stream a sample-data CSV row by row.

  A full (non-subset) run is ~1.2 M rows, so nothing here reads a value file into
  memory whole.
  """
  with path.open(encoding=SAMPLE_DATA_ENCODING, newline="") as handle:
    yield from csv.DictReader(handle, delimiter=SAMPLE_DATA_DELIMITER)


def _parse_timestamp(stamp: str) -> datetime:
  return datetime.strptime(stamp, SAMPLE_TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def _lang_struct(row: dict[str, str], prefix: str) -> dict[str, str]:
  """Collect a `<prefix>_de` / `_fr` / `_it` / `_en` column family into one JSONB struct."""
  return {language: row[f"{prefix}_{language}"] for language in LANGUAGES}


def _optional(value: str) -> str | None:
  """Map the export's empty strings to NULL.

  station_abbr and postal_code are each populated for some point types only, and
  the export leaves the others blank rather than absent.
  """
  return value.strip() or None


def _load_parameters() -> dict[str, dict]:
  """Index the parameter metadata by shortname, with its language structs prebuilt."""
  return {
    row["parameter_shortname"]: {
      "parameter_unit": row["parameter_unit"],
      "parameter_description": _lang_struct(row, "parameter_description"),
      "parameter_group": _lang_struct(row, "parameter_group"),
    }
    for row in _iter_csv(PARAMETER_META_CSV)
  }


def _load_points() -> dict[tuple[int, int], dict]:
  """Index the point metadata by (point_id, point_type), the export's real key.

  point_id alone is not unique -- the same id recurs across point types. The
  export also ships a handful of exact duplicate rows; keying them into a dict
  collapses those.

  Both coordinate pairs are read straight from the export, which publishes each
  point in WGS84 *and* LV95. Nothing here reprojects: the LV95 table is seeded
  from MeteoSwiss's own easting/northing rather than from a transform of the
  degrees, so neither table's coordinates are derived from the other's.
  """
  return {
    (int(row["point_id"]), int(row["point_type_id"])): {
      "point_name": row["point_name"],
      "point_type_name": _lang_struct(row, "point_type"),
      "station_abbr": _optional(row["station_abbr"]),
      "postal_code": _optional(row["postal_code"]),
      "point_height_masl": float(row["point_height_masl"]),
      "lon": float(row["point_coordinates_wgs84_lon"]),
      "lat": float(row["point_coordinates_wgs84_lat"]),
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

  `created` is the forecast *run* time, so it is identical for every row of a
  file: one run emits all of its steps at once. What varies within a point is
  forecast_datetime and value -- which is what makes the sample data useful for
  exercising OGC API `datetime=` filtering.

  The language structs are shared references, not copies: every row of a file
  carries the same parameter, and point_type_name comes from the point lookup.
  """
  parameters = _load_parameters()
  points = _load_points()
  undocumented = 0

  for path, shortname, run in _forecast_files():
    parameter = parameters.get(shortname)
    if parameter is None:
      raise RuntimeError(f"{path.name}: {PARAMETER_META_CSV.name} has no row for parameter {shortname}")

    for row in _iter_csv(path):
      point_id = int(row["point_id"])
      point_type = int(row["point_type_id"])
      point = points.get((point_id, point_type))
      if point is None:
        # No metadata means no coordinates, and geom is NOT NULL. The full export
        # has a few such points; the committed subset has none.
        undocumented += 1
        continue
      yield {
        "parameter_shortname": shortname,
        "value": float(row[shortname]),
        "point_id": point_id,
        "point_type": point_type,
        "forecast_datetime": _parse_timestamp(row["Date"]),
        "created": run,
        **parameter,
        **point,
      }

  if undocumented:
    logger.warning("skipped %d forecast value(s) whose point has no metadata, hence no coordinates", undocumented)


def _external_id(feature: dict) -> str:
  """Compose the primary key from the fields that identify a single reading.

  ``<point_id>_<point_type>_<parameter_shortname>_<forecast_datetime>``, where
  the timestamp is normalised to UTC and rendered as ``YYYYMMDDHHMMSS`` so the
  id carries none of the ISO-8601 separators::

      1_1_tre200h0_20260819210000

  point_id is not unique on its own in the source data -- the same id recurs
  across point types -- so point_type is part of the key rather than decoration.
  """
  compact = feature["forecast_datetime"].astimezone(UTC).strftime("%Y%m%d%H%M%S")
  return f"{feature['point_id']}_{feature['point_type']}_{feature['parameter_shortname']}_{compact}"


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
      cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(role)), (password,))
    else:
      logger.info("creating role %s", role)
      cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(role)), (password,))

    # CREATE DATABASE ... OWNER <role> requires the executing user to be a member of <role>
    # ("must be able to SET ROLE"). A real superuser bypasses this, but the RDS master user is
    # only rds_superuser, so it needs an explicit membership grant.
    #
    # This GRANT is unconditional on purpose. pg_has_role() already reports true for a role the
    # current user just created (postgres >= 16 records implicit CREATEROLE ownership), but that
    # implicit membership does NOT satisfy CREATE DATABASE ... OWNER -- so gating the GRANT on it
    # skips the grant exactly when it is needed. Re-granting an existing membership is a no-op.
    cur.execute("SELECT CURRENT_USER")
    current_user = cur.fetchone()[0]
    if current_user != role:
      logger.info("granting role %s to %s", role, current_user)
      cur.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(role), sql.Identifier(current_user)))


def ensure_database(conn: connection, dbname: str, owner: str) -> None:
  # CREATE DATABASE cannot run inside a transaction, hence ISOLATION_LEVEL_AUTOCOMMIT
  # in _connect() and the bare (non-context-manager) connection in main().
  with conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    if cur.fetchone():
      logger.info("database %s already exists", dbname)
      return
    logger.info("creating database %s owned by %s", dbname, owner)
    cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(dbname), sql.Identifier(owner)))


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
  """Recreate the feature tables and indexes, and optionally seed sample data.

  Each table in TABLES is dropped first, so any existing rows are discarded.

  Connects as the owner role rather than the admin user so that the objects are
  owned by the role pygeoapi actually connects as -- otherwise every table would
  belong to the RDS master user and the service would need extra grants.
  """
  conn = _connect(dbname, user=owner, password=owner_password)
  try:
    with conn.cursor() as cur:
      for table, srid, coord_keys in TABLES:
        logger.warning("dropping table %s and all its rows", table)
        cur.execute(_drop_table_sql(table))
        logger.info("creating table %s (srid %d)", table, srid)
        cur.execute(_create_table_sql(table, srid))
        for statement in _create_index_sql(table):
          cur.execute(statement)

        if seed:
          _seed_sample_data(cur, table, srid, coord_keys)

      if not seed:
        logger.info("skipping sample data (set DB_SEED_SAMPLE_DATA=true to enable)")
  finally:
    conn.close()


def _seed_sample_data(cur: cursor, table: str, srid: int, coord_keys: tuple[str, str]) -> None:
  """Insert the local-dev sample features, leaving existing rows untouched.

  `coord_keys` names the pair of feature keys holding this table's coordinates --
  ("lon", "lat") for WGS84, ("east", "north") for LV95 -- both of which come
  straight from the export. The rows are otherwise identical between tables.
  """
  logger.info("seeding %s from %s", table, SAMPLE_DATA_DIR)

  statement = sql.SQL(
    "INSERT INTO {table} ("
    "external_id, parameter_shortname, parameter_description, parameter_group, "
    "parameter_unit, value, point_id, point_type, point_name, point_type_name, "
    "station_abbr, postal_code, point_height_masl, forecast_datetime, created, geom"
    ") VALUES %s "
    "ON CONFLICT (external_id) DO NOTHING"
  ).format(table=sql.Identifier(table))

  # The geometry is assembled from bound x/y parameters rather than a per-row SQL
  # fragment, so no part of the sample data is ever interpolated into the
  # statement text. srid is an int constant from TABLES, never user input.
  template = "(" + ", ".join(["%s"] * 15) + f", ST_SetSRID(ST_MakePoint(%s, %s), {srid}))"

  x_key, y_key = coord_keys

  # Counted while streaming rather than up front: the row count is a property of
  # the CSVs, and nothing needs them all in memory to find it out.
  seeded = 0

  def rows() -> Iterator[tuple]:
    nonlocal seeded
    for feature in iter_sample_features():
      seeded += 1
      yield (
        _external_id(feature),
        feature["parameter_shortname"],
        json.dumps(feature["parameter_description"]),
        json.dumps(feature["parameter_group"]),
        feature["parameter_unit"],
        feature["value"],
        feature["point_id"],
        feature["point_type"],
        feature["point_name"],
        json.dumps(feature["point_type_name"]),
        feature["station_abbr"],
        feature["postal_code"],
        feature["point_height_masl"],
        feature["forecast_datetime"],
        feature["created"],
        feature[x_key],
        feature[y_key],
      )

  extras.execute_values(cur, statement, rows(), template=template, page_size=SEED_BATCH_SIZE)
  logger.info("seeded %d sample feature(s) into %s", seeded, table)


def main() -> int:
  logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)

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

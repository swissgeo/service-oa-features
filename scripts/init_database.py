"""Bootstrap the PostGIS database for service-oa-features.

Run as an init container before the app starts (see swissgeo/infra-kubernetes
services/service-oa-features/base/patch-init-database.yaml), and as the
`init-database` service in docker-compose.yml for local development.

The shared `services` RDS instance is only reachable from inside the VPC, so
terraform cannot create the database, its owner role or the PostGIS extension
(see swissgeo/infra-terraform aws/modules/common/aws-rds-postgres/README.md).
Terraform owns the credentials only; this script does the rest with the admin
credentials mounted from `service-oa-features-secrets-db-admin`.

Every step is idempotent, so this may re-run on every pod start.

Work is split across two identities on purpose:

* the admin user (`DB_ADMIN_USER`, an rds_superuser) creates the role, the
  database and the PostGIS extension -- all of which need privileges the owner
  role does not have;
* the owner role (`DB_USER`) creates the table and indexes, so that the role
  pygeoapi connects as actually owns the schema objects it reads and writes.

Seed data is local-dev only and is skipped unless DB_SEED_SAMPLE_DATA is set to
a truthy value; the sample Swiss features have no business being in a deployed
database.
"""

import json
import logging
import os
import sys

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT, connection, cursor

logger = logging.getLogger("init_database")

# The RDS master user is a member of rds_superuser, which is required to
# CREATE EXTENSION postgis.
ADMIN_DBNAME = "postgres"

TABLE_NAME = "swissgeo_features"

# title/description are JSONB language structs ({"de": …, "fr": …}); the
# provider collapses them to the requested language via pygeoapi's l10n.
CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
  external_id TEXT PRIMARY KEY,
  title       JSONB NOT NULL,
  description JSONB,
  theme       TEXT,
  created     TIMESTAMPTZ NOT NULL DEFAULT now(),
  geom        GEOMETRY(Geometry, 4326) NOT NULL
)
"""

CREATE_INDEX_SQL = (
  f"CREATE INDEX IF NOT EXISTS {TABLE_NAME}_geom_idx ON {TABLE_NAME} USING GIST (geom)",
  f"CREATE INDEX IF NOT EXISTS {TABLE_NAME}_created_idx ON {TABLE_NAME} (created)",
)

# Sample features for local development. geom_sql is a PostGIS constructor
# expression rather than a bound parameter, since each row uses a different one.
SAMPLE_FEATURES = (
  {
    "external_id": "ch.swisstopo.bern",
    "title": {"de": "Bern", "fr": "Berne", "it": "Berna", "en": "Bern"},
    "description": {
      "de": "Bundesstadt der Schweiz",
      "fr": "Ville fédérale de la Suisse",
      "it": "Città federale della Svizzera",
      "en": "Federal city of Switzerland",
    },
    "theme": "places",
    "created": "2026-01-15T10:00:00Z",
    "geom_sql": "ST_SetSRID(ST_MakePoint(7.4474, 46.9480), 4326)",
  },
  {
    "external_id": "ch.swisstopo.zurich",
    "title": {"de": "Zürich", "fr": "Zurich", "it": "Zurigo", "en": "Zurich"},
    "description": {
      "de": "Grösste Stadt der Schweiz",
      "fr": "Plus grande ville de Suisse",
      "it": "La città più grande della Svizzera",
      "en": "Largest city of Switzerland",
    },
    "theme": "places",
    "created": "2026-01-16T10:00:00Z",
    "geom_sql": "ST_SetSRID(ST_MakePoint(8.5417, 47.3769), 4326)",
  },
  {
    "external_id": "ch.swisstopo.geneve",
    "title": {"de": "Genf", "fr": "Genève", "it": "Ginevra", "en": "Geneva"},
    "description": {
      "de": "Stadt am Genfersee",
      "fr": "Ville au bord du lac Léman",
      "it": "Città sul lago di Ginevra",
      "en": "City on Lake Geneva",
    },
    "theme": "places",
    "created": "2026-01-17T10:00:00Z",
    "geom_sql": "ST_SetSRID(ST_MakePoint(6.1432, 46.2044), 4326)",
  },
  {
    "external_id": "ch.swisstopo.matterhorn",
    "title": {"de": "Matterhorn", "fr": "Cervin", "it": "Cervino", "en": "Matterhorn"},
    "description": {
      "de": "Berg in den Walliser Alpen",
      "fr": "Montagne des Alpes valaisannes",
      "it": "Montagna delle Alpi Vallesane",
      "en": "Mountain in the Valais Alps",
    },
    "theme": "mountains",
    "created": "2026-01-18T10:00:00Z",
    "geom_sql": "ST_SetSRID(ST_MakePoint(7.6586, 45.9763), 4326)",
  },
  {
    "external_id": "ch.swisstopo.lac-leman",
    "title": {"de": "Genfersee", "fr": "Lac Léman", "it": "Lago di Ginevra", "en": "Lake Geneva"},
    "description": {
      "de": "Grösster See der Schweiz",
      "fr": "Plus grand lac de Suisse",
      "it": "Il lago più grande della Svizzera",
      "en": "Largest lake of Switzerland",
    },
    "theme": "water",
    "created": "2026-01-19T10:00:00Z",
    "geom_sql": "ST_SetSRID(ST_MakeEnvelope(6.1500, 46.2100, 6.9000, 46.5100, 4326), 4326)",
  },
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
  """Create the feature table and indexes, and optionally seed sample data.

  Connects as the owner role rather than the admin user so that the objects are
  owned by the role pygeoapi actually connects as -- otherwise every table would
  belong to the RDS master user and the service would need extra grants.
  """
  conn = _connect(dbname, user=owner, password=owner_password)
  try:
    with conn.cursor() as cur:
      logger.info("ensuring table %s", TABLE_NAME)
      cur.execute(CREATE_TABLE_SQL)
      for statement in CREATE_INDEX_SQL:
        cur.execute(statement)

      if seed:
        _seed_sample_data(cur)
      else:
        logger.info("skipping sample data (set DB_SEED_SAMPLE_DATA=true to enable)")
  finally:
    conn.close()


def _seed_sample_data(cur: cursor) -> None:
  """Insert the local-dev sample features, leaving existing rows untouched."""
  logger.info("seeding %d sample features", len(SAMPLE_FEATURES))
  for feature in SAMPLE_FEATURES:
    # geom_sql is a trusted constant from SAMPLE_FEATURES, never user input.
    statement = sql.SQL(
      "INSERT INTO {table} (external_id, title, description, theme, created, geom) "
      "VALUES (%s, %s, %s, %s, %s, {geom}) "
      "ON CONFLICT (external_id) DO NOTHING"
    ).format(table=sql.Identifier(TABLE_NAME), geom=sql.SQL(feature["geom_sql"]))
    cur.execute(
      statement,
      (
        feature["external_id"],
        json.dumps(feature["title"]),
        json.dumps(feature["description"]),
        feature["theme"],
        feature["created"],
      ),
    )


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

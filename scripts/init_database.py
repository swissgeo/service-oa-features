"""Bootstrap the PostGIS database for service-oa-features.

Run as an init container before the app starts (see swissgeo/infra-kubernetes
services/service-oa-features/base/patch-init-database.yaml), and as the
`init-database` service in docker-compose.yml for local development.

The shared `services` RDS instance is only reachable from inside the VPC, so
terraform cannot create the database, its owner role or the PostGIS extension
(see swissgeo/infra-terraform aws/modules/common/aws-rds-postgres/README.md).
Terraform owns the credentials only; this script does the rest with the admin
credentials mounted from `service-oa-features-secrets-db-admin`.

Every step is idempotent and may re-run on every pod start.

No tables are created here. The MeteoSwiss OGD local-forecasting tables
(`tbl_ogd-local-forecasting-*`) are created and filled by the forecast pipeline,
and served by the collection discovery (swissgeo_oaf/discovery.py). The sample
table is created by the app on startup (swissgeo_oaf/sample_data.py).

The admin user (`DB_ADMIN_USER`, an rds_superuser) does all of it: the role, the
database and the PostGIS extension all need privileges the owner role
(`DB_USER`) does not have. The owner role is granted the public schema, so the
tables it creates belong to the role pygeoapi connects as.
"""

import logging
import os
import sys

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT, connection

logger = logging.getLogger("init_database")

# The RDS master user is a member of rds_superuser, which is required to
# CREATE EXTENSION postgis.
ADMIN_DBNAME = "postgres"


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


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

            # Let the owner role create its tables in the public schema.
            cur.execute(sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(sql.Identifier(owner)))
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    dbname = _env("DB_NAME")
    owner = _env("DB_USER")
    owner_password = _env("DB_PW")

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

    logger.info("database bootstrap complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

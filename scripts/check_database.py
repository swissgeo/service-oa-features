"""Check that the PostGIS database pygeoapi connects to is up and bootstrapped.

Used by `make check-db` before serving locally. Reads the same DB_* variables as
pygeoapi-config.yml and exits non-zero with a hint when the database is not
reachable or a table is missing.

Usage:
    python scripts/check_database.py TABLE [TABLE ...]
"""

import os
import sys

import psycopg2


def main(tables: list[str]) -> int:
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    dbname = os.environ.get("DB_NAME", "service-oa-features")
    where = f"{host}:{port}/{dbname}"
    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=os.environ.get("DB_USER", "service-oa-features"),
            password=os.environ.get("DB_PW", "swissgeo"),
            connect_timeout=3,
        )
    except psycopg2.OperationalError as err:
        print(f"ERROR: no PostGIS at {where}: {str(err).strip()}", file=sys.stderr)
        print("       Run `make services-up` first.", file=sys.stderr)
        return 1

    try:
        with conn.cursor() as cur:
            missing = []
            for table in tables:
                # quote_ident so names that need quoting (e.g. ones with hyphens,
                # like tbl_ogd-local-forecasting-tre200px) resolve rather than error.
                cur.execute("SELECT to_regclass(quote_ident(%s))", (table,))
                row = cur.fetchone()
                if row is None or row[0] is None:
                    missing.append(table)
    finally:
        conn.close()

    if missing:
        print(
            f"ERROR: PostGIS at {where} is up but missing table(s): {' '.join(missing)}",
            file=sys.stderr,
        )
        print("       Run `make reset-db` to create them.", file=sys.stderr)
        return 1

    print(f"PostGIS OK at {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Discover PostGIS tables and serve each one as a pygeoapi collection.

pygeoapi only knows the collections listed under ``resources`` in its config file,
and reads that file once, when ``pygeoapi.starlette_app`` is imported. So the
discovery runs before that import (see ``app.py``):

1. read the config file named by ``PYGEOAPI_CONFIG``, including its non-pygeoapi
   ``swissgeo.discovery`` section;
2. for each discovery rule, list the PostGIS geometry tables in the provider's
   schema whose name matches the rule's glob, minus its excludes;
3. add one resource per table to ``resources``, built from the rule's template;
4. write the result next to the OpenAPI document, generate the OpenAPI document
   from it, and point ``PYGEOAPI_CONFIG`` at the generated file.

A table created later is picked up by ``swissgeo_oaf.refresh``, which repeats the
discovery periodically in the running app and swaps the new collections in.

The generated file is written from the config *before* ``${VAR}`` interpolation,
so database credentials stay environment references and never land on disk.

Example rule::

    swissgeo:
      discovery:
        - tables: tbl_ogd-local-forecasting-*
          exclude: [tbl_ogd-local-forecasting-tmp-*]
          collection_id: ogd-local-forecasting-{match}
          title_columns: parameter_description
          resource:
            type: collection
            title: Local forecast {match}
            ...
            providers:
              - type: feature
                name: swissgeo_oaf.provider.postgresql.SwissGeoProvider
                data: {...}
                # no `table`/`resource_id`: set per discovered table

``{match}`` is the part of the table name matched by the glob's ``*`` (``tre200px``
for ``tbl_ogd-local-forecasting-tre200px``) and ``{table}`` the full name; both
can be used in ``collection_id`` and in the resource's ``title`` and
``description``.

``title_columns`` (optional) names a ``<prefix>_<lang>`` column family to take the
collection's title from, as a pygeoapi language struct, e.g.
``parameter_description`` -> ``{"en": "Air temperature …", "de": …}``. It is read
from the first row of the table, and the template title is kept when the table
is empty or lacks the columns.
"""

import copy
import fnmatch
import logging
import os
import re
import sys
from pathlib import Path

import psycopg2
import yaml
from psycopg2 import sql
from psycopg2.extensions import connection
from pygeoapi.openapi import generate_openapi_document
from pygeoapi.util import yaml_load

LOGGER = logging.getLogger(__name__)

GENERATED_CONFIG_NAME = "pygeoapi-config.generated.yml"

_LANGUAGES = ("en", "de", "fr", "it")

# Strings of the resource template that the {match}/{table} placeholders apply to.
# Deliberately not every string: the provider's `${DB_HOST:-…}` references contain
# braces too and must reach pygeoapi untouched.
_FORMATTED_KEYS = ("title", "description")

_CONNECT_TIMEOUT_SECONDS = 10


class _NoAliasDumper(yaml.SafeDumper):
    """Write shared objects (YAML anchors in the source config) out in full.

    The source config reuses the discovery template through anchors, so the parsed
    document shares objects; the default dumper would emit them as &id001 aliases.
    """

    def ignore_aliases(self, data: object) -> bool:  # noqa: ARG002
        return True


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Compile a ``*``-only glob, capturing what its first ``*`` matched.

    Without a ``*`` the capture is empty. Other glob syntax (``?``, ``[…]``) is
    taken literally -- table names are matched by prefix/suffix only.
    """
    head, *rest = pattern.split("*")
    regex = re.escape(head)
    if not rest:
        return re.compile(regex + "()")
    for index, part in enumerate(rest):
        regex += ("(.+)" if index == 0 else ".*") + re.escape(part)
    return re.compile(regex)


def match_tables(tables: list[str], pattern: str, exclude: list[str]) -> list[tuple[str, str]]:
    """Return ``(table, match)`` for each of *tables* matching the glob *pattern*.

    *match* is what the first ``*`` of the pattern matched. Tables matching one
    of the *exclude* globs (fnmatch syntax) are left out. The result is sorted by
    table name.
    """
    regex = _glob_to_regex(pattern)
    matched = []
    for table in sorted(tables):
        if any(fnmatch.fnmatchcase(table, excluded) for excluded in exclude):
            continue
        found = regex.fullmatch(table)
        if found:
            matched.append((table, found.group(1)))
    return matched


def _format(value: object, placeholders: dict[str, str]) -> object:
    """Fill the placeholders in a string or in every string of a language struct."""
    if isinstance(value, str):
        return value.format(**placeholders)
    if isinstance(value, dict):
        return {key: _format(item, placeholders) for key, item in value.items()}
    return value


def build_resource(
    template: dict,
    collection_id: str,
    table: str,
    match: str,
    title: dict[str, str] | None = None,
) -> dict:
    """Instantiate the resource *template* for one discovered *table*.

    Each provider gets the table, and the collection id as its ``resource_id``,
    which SwissGeoProvider builds the items' ``rel=self`` links from.
    """
    resource = copy.deepcopy(template)
    placeholders = {"table": table, "match": match}
    for key in _FORMATTED_KEYS:
        if key in resource:
            resource[key] = _format(resource[key], placeholders)
    if title:
        resource["title"] = title
    for provider in resource.get("providers", []):
        provider["table"] = table
        provider["resource_id"] = collection_id
    return resource


def _schema_of(provider: dict) -> str:
    search_path = provider.get("data", {}).get("search_path") or ["public"]
    return search_path[0]


def _connect(provider: dict) -> connection:
    """Connect with the same settings pygeoapi's PostgreSQLProvider would use."""
    data = provider["data"]
    options = provider.get("options", {})
    return psycopg2.connect(
        host=data.get("host"),
        port=data.get("port"),
        dbname=data.get("dbname"),
        user=data.get("user"),
        password=data.get("password"),
        sslmode=options.get("sslmode", "prefer"),
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
    )


def _geometry_tables(conn: connection, schema: str) -> list[str]:
    """List the tables of *schema* that have a PostGIS geometry column."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT f_table_name FROM geometry_columns WHERE f_table_schema = %s",
            (schema,),
        )
        return [row[0] for row in cur.fetchall()]


def _lang_title(conn: connection, schema: str, table: str, prefix: str) -> dict[str, str] | None:
    """Read a ``<prefix>_<lang>`` language struct off the first row of *table*."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        existing = {row[0] for row in cur.fetchall()}
        languages = [lang for lang in _LANGUAGES if f"{prefix}_{lang}" in existing]
        if not languages:
            return None
        cur.execute(
            sql.SQL("SELECT {columns} FROM {table} LIMIT 1").format(
                columns=sql.SQL(", ").join(
                    sql.Identifier(f"{prefix}_{lang}") for lang in languages
                ),
                table=sql.Identifier(schema, table),
            )
        )
        row = cur.fetchone()
    if row is None:
        return None
    title = {lang: value for lang, value in zip(languages, row, strict=True) if value}
    return title or None


def _feature_provider(resource: dict) -> dict:
    for provider in resource.get("providers", []):
        if provider.get("type") == "feature":
            return provider
    raise ValueError("discovery resource template has no feature provider")


def _rule_tables(conn: connection, schema: str, rule: dict) -> list[tuple[str, str]]:
    return match_tables(_geometry_tables(conn, schema), rule["tables"], rule.get("exclude", []))


def matched_tables(raw_config: dict, config: dict) -> tuple[tuple[str, ...], ...]:
    """Return the tables each discovery rule matches, one tuple per rule.

    A cheap fingerprint of what :func:`discover` would serve: it only lists the
    tables, without reading their titles.
    """
    rules = raw_config.get("swissgeo", {}).get("discovery", [])
    interpolated_rules = config.get("swissgeo", {}).get("discovery", [])
    fingerprint = []
    for rule, interpolated in zip(rules, interpolated_rules, strict=True):
        provider = _feature_provider(interpolated["resource"])
        schema = _schema_of(provider)
        conn = _connect(provider)
        try:
            fingerprint.append(tuple(table for table, _ in _rule_tables(conn, schema, rule)))
        finally:
            conn.close()
    return tuple(fingerprint)


def discover(raw_config: dict, config: dict) -> dict:
    """Return *raw_config* with a resource added per table the discovery rules match.

    *config* is the same document with ``${VAR}`` references interpolated; it
    supplies the database connection. *raw_config* is what the resources are built
    from, so that the references survive into the generated file.
    """
    rules = raw_config.get("swissgeo", {}).get("discovery", [])
    interpolated_rules = config.get("swissgeo", {}).get("discovery", [])
    result = copy.deepcopy(raw_config)
    resources = result.setdefault("resources", {}) or {}
    result["resources"] = resources

    for rule, interpolated in zip(rules, interpolated_rules, strict=True):
        provider = _feature_provider(interpolated["resource"])
        schema = _schema_of(provider)
        conn = _connect(provider)
        try:
            tables = _rule_tables(conn, schema, rule)
            if not tables:
                LOGGER.warning("discovery: no table in %s matches %s", schema, rule["tables"])
            for table, match in tables:
                collection_id = rule.get("collection_id", "{table}").format(
                    table=table, match=match
                )
                if collection_id in resources:
                    LOGGER.warning(
                        "discovery: collection %s (table %s) is already configured, skipping",
                        collection_id,
                        table,
                    )
                    continue
                title_columns = rule.get("title_columns")
                title = _lang_title(conn, schema, table, title_columns) if title_columns else None
                resources[collection_id] = build_resource(
                    rule["resource"], collection_id, table, match, title
                )
                LOGGER.info("discovery: serving table %s as collection %s", table, collection_id)
        finally:
            conn.close()

    return result


def generated_config_path(openapi_path: Path) -> Path:
    return openapi_path.with_name(GENERATED_CONFIG_NAME)


def load_source(source: Path) -> tuple[dict, dict]:
    """Read the hand-written config, as is and with ``${VAR}`` references interpolated."""
    with source.open(encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle)
    with source.open(encoding="utf-8") as handle:
        config = yaml_load(handle)
    return raw_config, config


def generate(source: Path, openapi_path: Path, raw_config: dict, config: dict) -> Path:
    """Run the discovery and write the generated config and OpenAPI document.

    Returns the path of the generated config, next to *openapi_path*.
    """
    target = generated_config_path(openapi_path)
    generated = discover(raw_config, config)
    target.write_text(
        "# Generated by swissgeo_oaf.discovery -- do not edit.\n"
        f"# Edit {source.name} instead.\n"
        + yaml.dump(generated, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    # Annotated as the OAPIFormat model, but compared against the plain string.
    openapi = generate_openapi_document(target, "yaml")  # ty: ignore[invalid-argument-type]
    openapi_path.write_text(openapi, encoding="utf-8")
    LOGGER.info("discovery: wrote %s and %s", target, openapi_path)
    return target


def prepare_config() -> Path:
    """Run the discovery and point pygeoapi at the generated config and OpenAPI document.

    Must run before ``pygeoapi.starlette_app`` is imported. uvicorn's --reload
    re-imports the app in a fresh child process, whose PYGEOAPI_CONFIG still names
    the source config, so every run starts from the hand-written file.
    """
    source = Path(os.environ["PYGEOAPI_CONFIG"])
    openapi_path = Path(os.environ["PYGEOAPI_OPENAPI"])
    target = generate(source, openapi_path, *load_source(source))
    os.environ["PYGEOAPI_CONFIG"] = str(target)
    return target


def main() -> int:  # pragma: no cover
    """Generate the config and OpenAPI document without starting the app."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    prepare_config()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

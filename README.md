# service-oa-features

OGC API Features service for SwissGeo, built on [pygeoapi](https://pygeoapi.io/) with a PostGIS backend and multilingual feature support.

| Branch | Status | Coverage |
|--------|-----------|-------|
| develop | ![Build Status](CODEBUILD_BADGE_URL) | [![codecov-develop](https://codecov.io/gh/swissgeo/service-oa-features/branch/develop/graph/badge.svg)](https://codecov.io/gh/swissgeo/service-oa-features) |
| main | ![Build Status](CODEBUILD_BADGE_URL) | [![codecov-main](https://codecov.io/gh/swissgeo/service-oa-features/branch/main/graph/badge.svg)](https://codecov.io/gh/swissgeo/service-oa-features) |

## Table of Content
- [Table of Content](#table-of-content)
- [Overview](#overview)
- [Language handling](#language-handling)
  - [Why `app.py` is needed](#why-apppy-is-needed)
  - [Link patching](#link-patching)
- [Configuration](#configuration)
  - [Collection discovery](#collection-discovery)
- [Synthetic check](#synthetic-check)
- [Running locally](#running-locally)
  - [Setup](#setup)
  - [Start the backing services](#start-the-backing-services)
  - [Run on the host](#run-on-the-host)
  - [Run the built image](#run-the-built-image)
- [Database](#database)
- [Debugging](#debugging)
- [Development](#development)
- [Project structure](#project-structure)

## Overview

This service exposes Swiss geospatial data as an OGC API Features endpoint. pygeoapi handles the OGC API layer; features are stored in PostGIS and queried via `SwissGeoProvider`, a custom provider that adds language-aware field selection and link patching on top of pygeoapi's built-in `PostgreSQLProvider`.

```
uvicorn (swissgeo_oaf/app.py), at startup:
  ├─ swissgeo_oaf.sample_data – recreates and seeds the sample_mch_param table
  └─ swissgeo_oaf.discovery – one collection per tbl_ogd-local-forecasting-* table,
                              added to the static ones (sample-mch-param) and
                              written to the generated pygeoapi config + OpenAPI doc

Client
  │  ?lang=de&f=json
  ▼
uvicorn (swissgeo_oaf/app.py)
  │                       ← patches call_api_threadsafe to inject lang/fmt
  │                          into the executor thread-local before each call
  │                       ← Cache-Control and disable-HTML middleware
  ▼
pygeoapi Starlette app
  │
  ▼
SwissGeoProvider          ← swissgeo_oaf.provider.postgresql, extends PostgreSQLProvider
  │  query() / get()
  ├─ reads lang from thread-local (set by app.py)
  ├─ calls super().query() / super().get()
  ├─ _translate_props() – collapses the per-language columns
  └─ _patch_links()     – appends ?lang=…&f=… to same-host links
  │
  ▼
PostGIS  (tbl_ogd-local-forecasting-* tables + sample_mch_param, LV95)
```

## Language handling

`parameter_description`, `parameter_group` and `point_type` are stored as one column per language, as the MeteoSwiss source ships them (`point_name` is a single column, as the source ships one name per point):

```
point_type_de | point_type_fr | point_type_it | point_type_en
Station       | Station       | Stazione      | Station
```

`SwissGeoProvider._translate_props()` collapses each family into a single property (`point_type`, `parameter_description`, `parameter_group`) holding the language pygeoapi resolves, using pygeoapi's own `l10n.translate` so behaviour matches the rest of the framework: the requested language wins, then the first column present in the order `en`, `de`, `fr`, `it`. Supported languages: `en`, `de`, `fr`, `it` (falls back to `en`).

The collapsing happens on the way out only. Queryables, `properties=` and `sortby=` still see the per-language column names (e.g. `sortby=point_type_de`).

### Why `app.py` is needed

pygeoapi's Starlette integration runs provider calls in a thread pool. By the time the provider executes, the Starlette request context is no longer accessible. `app.py` monkey-patches `call_api_threadsafe` to call `set_request_params(lang, fmt)` just before dispatching each call, storing the values in a `threading.local` that `SwissGeoProvider` reads.

It also works around a pygeoapi bug where `request.raw_locale` is a plain string: `get_plugin_locale` passes it straight to `l10n.best_match`, which only accepts a list or `Locale`, so the provider locale would always fall back to `en` regardless of `?lang=`. `app.py` rewrites `_raw_locale` to a one-element list.

### Link patching

`_patch_links()` appends `?lang=<lang>&f=<fmt>` to any link whose `href` is relative or starts with the server base URL (`PYGEOAPI_HOSTNAME` + `API_PREFIX`). External links are left untouched.

`query()` also inserts a `rel=self` link per feature, which pygeoapi does not add on the items-list path. `get()` deliberately does **not**, because pygeoapi's `get_collection_item()` appends its own `rel=self` afterwards — adding one there would emit a duplicate.

## Configuration

### Collection discovery

The collections come from two places:

| Collection | Table | Source |
|---|---|---|
| `sample-mch-param` | `sample_mch_param` | listed statically under `resources:` in `pygeoapi-config.yml`; sample data the app seeds on startup, see [Database](#database) |
| `ogd-local-forecasting-<param>` | `tbl_ogd-local-forecasting-<param>` | discovered at startup |

The forecast pipeline creates and fills the `tbl_ogd-local-forecasting-*` tables, so they are
not listed in the config. At startup, before pygeoapi reads its config, `swissgeo_oaf.discovery`
serves every PostGIS table matching `tbl_ogd-local-forecasting-*` as a collection, except
`tbl_ogd-local-forecasting-point-meta`. A discovered collection never replaces a static one
with the same id.

The rule lives under the `swissgeo.discovery` key of `pygeoapi-config.yml`. pygeoapi ignores
that key. Each rule has a table glob, excludes, a collection id pattern and a resource template;
see the comments in the file. The static `sample-mch-param` reuses that template through YAML
anchors (`&forecast-resource`, `&forecast-provider`). The collection title is read from the table's
`parameter_description_<lang>` columns, so it is localised. Each provider gets the table and
the collection id as its `resource_id`, which the items' `rel=self` links are built from.

Discovery writes `pygeoapi-config.generated.yml` next to `PYGEOAPI_OPENAPI`, then generates
the OpenAPI document from it and points `PYGEOAPI_CONFIG` at it. That directory therefore has to
be writable. The file is written before `${VAR}` interpolation, so credentials stay env
references and never reach the disk. `make openapi` runs the same discovery without serving.

Worth knowing:

- **Tables are discovered at startup only.** A table created later is served after the next
  restart (`kubectl rollout restart`, or saving a file under `make serve --reload`).
- **Only tables with a PostGIS geometry column that the service role can `SELECT` are
  found.** PostGIS's `geometry_columns` view hides the others. A table created by another role
  needs a `GRANT SELECT` to `DB_USER`.
- **An unreachable database fails the startup**, rather than serving an API without
  collections. A database without matching tables starts with only `sample-mch-param`
  and logs a warning.

The resource template's provider in `pygeoapi-config.yml`:

```yaml
providers:
  - type: feature
    name: swissgeo_oaf.provider.postgresql.SwissGeoProvider
    data:
      host: ${DB_HOST:-localhost}
      port: ${DB_PORT:-5432}
      dbname: ${DB_NAME:-service-oa-features}
      user: ${DB_USER:-service-oa-features}
      password: ${DB_PW:-swissgeo}
      search_path: [public]
    options:
      sslmode: ${DB_SSLMODE:-prefer}
    # resource_id and table are set per discovered table.
    id_field: id
    geom_field: geometry
    time_field: datetime
    title_field: point_name
    crs:
      - http://www.opengis.net/def/crs/EPSG/0/2056
      - http://www.opengis.net/def/crs/OGC/1.3/CRS84
    storage_crs: http://www.opengis.net/def/crs/EPSG/0/2056
    languages:
      - en
      - de
      - fr
      - it
```

Key environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PYGEOAPI_HOSTNAME` | `http://localhost:8080` | Public host of the API; base of the patched links |
| `API_PREFIX` | `/api/oaf/rc1` | Path the API is mounted under; also part of the patched links |
| `DB_HOST` | `localhost` | PostGIS host |
| `DB_PORT` | `5432` | PostGIS port |
| `DB_NAME` | `service-oa-features` | PostGIS database name |
| `DB_USER` | `service-oa-features` | PostGIS user |
| `DB_PW` | `swissgeo` | PostGIS password |
| `DB_SSLMODE` | `prefer` | libpq `sslmode` |
| `SAMPLE_DATA_ENABLED` | `true` | Recreate and seed the `sample_mch_param` table on startup |
| `PYGEOAPI_CONFIG` | `/pygeoapi/pygeoapi-config.yml` | pygeoapi config file path |
| `THREADPOOL_MAX_WORKERS` | `16` | Threads running the API calls |
| `CACHE_CONTROL_HEADER` | `public, max-age=300` | `Cache-Control` header set on GET/HEAD responses, except 5xx and responses that already have one; empty disables it |
| `HTML_ENABLED` | `false` | Serve pygeoapi's HTML output; when `false`, `f=html` returns 400 and `text/html` is ignored in `Accept` (browsers get JSON) |
| `OTEL_PYTHON_EXCLUDED_URLS` | | Comma-separated URL regexes not traced by the ASGI middleware (e.g. `/syntheticz`) |
| `UVICORN_LIMIT_CONCURRENCY` | `100` (image) | Open connections/requests before uvicorn answers 503 |
| `MALLOC_ARENA_MAX` | `2` (image) | Limits glibc malloc arenas to reduce memory fragmentation |

pygeoapi shares one SQLAlchemy engine per process across all collections with the same
connection settings. Its pool defaults to 5 connections plus 10 overflow, so with the default 16
worker threads a burst of slow queries can make one thread wait for a connection.

The `PYGEOAPI_HOSTNAME` and `API_PREFIX` defaults appear twice: in `swissgeo_oaf/settings.py`
and in `server.url` of `pygeoapi-config.yml`, which pygeoapi reads itself.
`tests/test_settings.py` checks that they match.

## Synthetic check

`GET /api/oaf/rc1/syntheticz` checks that PostGIS is reachable and that the table of every
`SwissGeoProvider` collection, including the discovered ones, exists. It returns `200` with `"status": "UP"`, or
`500` with `"status": "DOWN"`, and is never cached:

```json
{"service": {"name": "service-oa-features", "version": "v0.3.0"}, "status": "UP", "external_systems": {"postgresql": {"status": "UP"}}}
```

The version is written into `swissgeo_oaf/version.py` when the image is built (`VERSION`
build argument, `git describe --tags` in `make dockerbuild`); it is `local` otherwise.

## Running locally

The app runs on the host; its backing services run in docker compose (`docker-compose.yml`):

- **PostGIS** (PostgreSQL 18) on port 5432, bootstrapped by the `init-database` service
- **OTel collector** on 4317 (gRPC) / 4318 (HTTP), **Prometheus** on `:9090` and **Jaeger** on `:16686`

### Setup

```bash
make setup
```

Creates the virtualenv with `uv sync` (which also installs the `swissgeo_oaf` package in
editable mode), installs the pre-commit hook that runs `make lint`, creates `.env` from `.env.default` if it is missing,
and drops you into a shell with those variables exported. `.env` is gitignored, so local
overrides (e.g. a different `DB_HOST`) survive. A `.env.local`, if present, takes
precedence over `.env`.

### Start the backing services

```bash
make services-up
```

Starts PostGIS, the collector, Jaeger and Prometheus, and runs the database bootstrap
(see [Database](#database)).

### Run on the host

```bash
make serve
```

Checks that PostGIS is reachable and bootstrapped (`make check-db`) and serves the app with
`--reload` on `http://localhost:8080/api/oaf/rc1/`. On startup the app discovers the
collections and generates the OpenAPI document (see [Collection discovery](#collection-discovery)).

Try it:

```bash
curl 'http://localhost:8080/api/oaf/rc1/collections?f=json'
curl 'http://localhost:8080/api/oaf/rc1/collections/sample-mch-param/items?lang=fr&f=json'
curl 'http://localhost:8080/api/oaf/rc1/collections/sample-mch-param/items?bbox=5.9,45.9,6.9,46.6&f=json'
curl 'http://localhost:8080/api/oaf/rc1/collections/sample-mch-param/items?crs=http://www.opengis.net/def/crs/EPSG/0/2056&f=json'
```

Locally only `sample-mch-param` exists until a `tbl_ogd-local-forecasting-*` table is
created, e.g. by the pipeline; restart the app to pick it up.

Without `crs=` the output is CRS84, per OGC API - Features Part 2, and `bbox` is read as CRS84
unless `bbox-crs` is passed too.

### Run the built image

```bash
make dockerrun
```

Builds the image and runs it with `--net=host`, so it reaches the same
`localhost:5432` / `localhost:4317` as `make serve`, with the `DB_*` values from your env file.
This is for verifying the deployment artifact; day-to-day development uses `make serve`.

## Database

- **`scripts/init_database.py`** creates the owner role, the database and the PostGIS extension,
  and grants the owner role the public schema. It creates no tables. Every step is idempotent.
  It runs locally (the `init-database` compose service, `make reset-db`) and in kubernetes, as
  an init container (see swissgeo/infra-kubernetes
  `services/service-oa-features/base/patch-init-database.yaml`).
- **The app itself** (`swissgeo_oaf/sample_data.py`) **drops, recreates and seeds** the
  `sample_mch_param` table behind the static `sample-mch-param` collection on every start,
  before pygeoapi loads. It connects with the app's own `DB_*` settings, so the table belongs
  to the service role. An advisory lock serialises pods starting together, and pods already
  serving wait on the table lock rather than seeing it missing. The table has the columns of
  the MeteoSwiss OGD local-forecasting tables, with geometry in LV95 (EPSG:2056) and indexes on
  `geometry` (GIST), `datetime` and `feature_id`. Its name keeps it out of the discovery. Set
  `SAMPLE_DATA_ENABLED=false` to skip it; then nothing creates the table and the collection
  fails.

The seed rows are read from the CSVs in `swissgeo_oaf/sample-data/`, shipped with the package.
They are a committed subset of the MeteoSwiss OGD local-forecasting export. See
`scripts/make_sample_data.py` for how to rebuild it from a full export.

The forecast pipeline creates and fills the `tbl_ogd-local-forecasting-*` tables. Their names
contain hyphens, so they must be double-quoted in SQL
(`SELECT * FROM "tbl_ogd-local-forecasting-tre200px"`).

```bash
make dbshell        # psql shell on the local PostGIS
make reset-db       # re-apply the bootstrap (role, database, PostGIS)
make reseed         # recreate the sample table without restarting the app
make services-down  # stop the backing services, keeping the PostGIS data volume
```

The compose PostGIS keeps its data in the `pgdata-18` volume. The volume of the earlier
PostgreSQL 17 setup (`pgdata`) is no longer used and can be removed with
`docker volume rm service-oa-features_pgdata`.

## Debugging

```bash
make serve-debug
```

Runs the app under [debugpy](https://github.com/microsoft/debugpy) listening on port 5678 and
waiting for a client, so you can attach before the first request is handled.

Then attach your debugger (e.g. **"Attach to Docker (swissgeo_oaf)"** in Zed) to
`localhost:5678`.

To debug the container instead, build the image with `--build-arg INSTALL_DEBUGPY=true`
(debugpy is not part of the production image) and run it with `PYDEBUG=true`.

## Development

```bash
make test    # pytest with an HTML coverage report
make lint    # ruff + ty
make format  # ruff format
```

## Project structure

```
swissgeo_oaf/               # Python package, installed in the virtualenv by `uv sync`
  app.py                    # Starlette entrypoint; patches call_api_threadsafe
  settings.py               # pydantic settings, read from the environment
  otel.py                   # OpenTelemetry setup
  cache_control.py          # Cache-Control middleware
  disable_html.py           # middleware disabling pygeoapi's HTML output
  syntheticz.py             # /syntheticz health check of PostGIS
  discovery.py              # startup discovery of the tables served as collections
  sample_data.py            # startup (re)seeding of the sample_mch_param table
  sample-data/              # Committed CSV subset it seeds from
  version.py                # `local`, overwritten with the version in the image
  schemas/                  # pydantic models of the API responses
  provider/                 # pygeoapi providers
    postgresql.py           # SwissGeoProvider: language selection + link patching
scripts/
  init_database.py          # DB/role/PostGIS bootstrap (also the kubernetes init container)
  check_database.py         # `make check-db`: is PostGIS up and bootstrapped?
  make_sample_data.py       # Dev tool: rebuilds swissgeo_oaf/sample-data/ from the full OGD export
tests/
pygeoapi-config.yml         # pygeoapi server config + collection discovery rules
config-files/               # uvicorn logging, OTel collector and Prometheus configuration
```

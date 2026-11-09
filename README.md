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
PostGIS  (tbl_ogd-local-forecasting-tre200px table, LV95)
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

Provider registration in `pygeoapi-config.yml`:

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
    resource_id: swissgeo-features
    table: tbl_ogd-local-forecasting-tre200px
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
| `PYGEOAPI_CONFIG` | `/pygeoapi/pygeoapi-config.yml` | pygeoapi config file path |
| `DB_SEED_SAMPLE_DATA` | unset | When truthy, `scripts/init_database.py` seeds the local sample features |
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
`SwissGeoProvider` in `pygeoapi-config.yml` exists. It returns `200` with `"status": "UP"`, or
`500` with `"status": "DOWN"`, and is never cached:

```json
{"service": {"name": "service-oa-features", "version": "v0.3.0"}, "status": "UP", "external_systems": {"postgresql": {"status": "UP"}}}
```

The version is written into `swissgeo_oaf/version.py` when the image is built (`VERSION`
build argument, `git describe --tags` in `make dockerbuild`); it is `local` otherwise.

## Running locally

The app runs on the host; its backing services run in docker compose (`docker-compose.yml`):

- **PostGIS** (PostgreSQL 18) on port 5432, bootstrapped by the `init-database` service with a small sample dataset
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

Starts PostGIS, the collector, Jaeger and Prometheus, and runs the `init-database` bootstrap
(see [Database](#database)), which **drops and reseeds** the tables.

### Run on the host

```bash
make serve
```

Checks that PostGIS is reachable and bootstrapped (`make check-db`), generates the OpenAPI
document and serves the app with `--reload` on `http://localhost:8080/api/oaf/rc1/`.

Try it:

```bash
curl 'http://localhost:8080/api/oaf/rc1/collections/sample-mch-param/items?lang=fr&f=json'
curl 'http://localhost:8080/api/oaf/rc1/collections/sample-mch-param/items?bbox=5.9,45.9,6.9,46.6&f=json'
curl 'http://localhost:8080/api/oaf/rc1/collections/sample-mch-param/items?crs=http://www.opengis.net/def/crs/EPSG/0/2056&f=json'
```

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

The schema lives in `scripts/init_database.py`, which runs both locally (the `init-database`
compose service, via `make services-up` / `make reset-db`) and in kubernetes (as an init container, see
swissgeo/infra-kubernetes `services/service-oa-features/base/patch-init-database.yaml`).

It creates the owner role, the database, the PostGIS extension and two tables mirroring the
MeteoSwiss OGD local-forecasting definitions, both with geometry in LV95 (EPSG:2056):

| Table | Content | Indexes |
|---|---|---|
| `tbl_ogd-local-forecasting-tre200px` | one row per forecast value, with its point and parameter metadata; backs `sample-mch-param` | `geometry` (GIST), `datetime`, `feature_id` |
| `tbl_ogd-local-forecasting-point-meta` | one row per forecast point | `geometry` (GIST) |

The table names contain hyphens, so they must be double-quoted in SQL
(`SELECT * FROM "tbl_ogd-local-forecasting-tre200px"`). The role, database and extension steps are
idempotent, but the tables are **dropped and recreated on every run**, so re-running it discards
all existing rows — including on an ordinary pod restart. The sample features are local-dev only
and are inserted only when `DB_SEED_SAMPLE_DATA` is truthy, which compose sets and the deployed
environments do not.

The seed rows are read from the CSVs in `scripts/sample-data/`, a committed subset of the
MeteoSwiss OGD local-forecasting export. See `scripts/make_sample_data.py` for how to rebuild it
from a full export.

```bash
make dbshell        # psql shell on the local PostGIS
make reset-db       # re-apply the bootstrap (drops and reseeds the tables)
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
  version.py                # `local`, overwritten with the version in the image
  schemas/                  # pydantic models of the API responses
  provider/                 # pygeoapi providers
    postgresql.py           # SwissGeoProvider: language selection + link patching
scripts/
  init_database.py          # DB/role/PostGIS/schema bootstrap + local seed data
  check_database.py         # `make check-db`: is PostGIS up with the expected tables?
  make_sample_data.py       # Dev tool: rebuilds sample-data/ from the full OGD export
  sample-data/              # Committed CSV subset the seeder reads
tests/
pygeoapi-config.yml         # pygeoapi server + collection configuration
config-files/               # uvicorn logging, OTel collector and Prometheus configuration
```

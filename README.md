# service-oa-features

OGC API Features service for SwissGeo, built on [pygeoapi](https://pygeoapi.io/) with a PostGIS backend and multilingual feature support.

| Branch | Status | Coverage |
|--------|-----------|-------|
| develop | ![Build Status](CODEBUILD_BADGE_URL) | [![codecov-develop](https://codecov.io/gh/swissgeo/service-oa-features/branch/develop/graph/badge.svg)](https://codecov.io/gh/swissgeo/service-oa-features) |
| main | ![Build Status](CODEBUILD_BADGE_URL) | [![codecov-main](https://codecov.io/gh/swissgeo/service-oa-features/branch/main/graph/badge.svg)](https://codecov.io/gh/swissgeo/service-oa-features) |

> [!NOTE]
> This is still in POC phase

## Overview

This service exposes Swiss geospatial data as an OGC API Features endpoint. pygeoapi handles the OGC API layer; features are stored in PostGIS and queried via `SwissGeoProvider`, a custom provider that adds language-aware field selection and link patching on top of pygeoapi's built-in `PostgreSQLProvider`.

```
Client
  │  ?lang=de&f=json
  ▼
uvicorn (app.py)          ← patches call_api_threadsafe to inject lang/fmt
  │                          into the executor thread-local before each call
  ▼
pygeoapi Starlette app
  │
  ▼
SwissGeoProvider          ← extends PostgreSQLProvider
  │  query() / get()
  ├─ reads lang from thread-local (set by app.py)
  ├─ calls super().query() / super().get()
  ├─ _translate_props() – collapses the JSONB language structs
  └─ _patch_links()     – appends ?lang=…&f=… to same-host links
  │
  ▼
PostGIS  (sample_features table)
```

## Language handling

`parameter_description`, `parameter_group` and `point_name` are stored as JSONB language structs:

```json
{"de": "Genf", "fr": "Genève", "it": "Ginevra", "en": "Geneva"}
```

`SwissGeoProvider._translate_props()` collapses them to the language pygeoapi resolves, using pygeoapi's own `l10n.translate` so behaviour matches the rest of the framework: the requested language wins, then the first available language, then the struct itself. Supported languages: `en`, `de`, `fr`, `it` (falls back to `en`).

### Why `app.py` is needed

pygeoapi's Starlette integration runs provider calls in a thread pool. By the time the provider executes, the Starlette request context is no longer accessible. `app.py` monkey-patches `call_api_threadsafe` to call `set_request_params(lang, fmt)` just before dispatching each call, storing the values in a `threading.local` that `SwissGeoProvider` reads.

It also works around a pygeoapi bug where `request.raw_locale` is a plain string: `get_plugin_locale` passes it straight to `l10n.best_match`, which only accepts a list or `Locale`, so the provider locale would always fall back to `en` regardless of `?lang=`. `app.py` rewrites `_raw_locale` to a one-element list.

### Link patching

`_patch_links()` appends `?lang=<lang>&f=<fmt>` to any link whose `href` is relative or starts with the server base URL. External links are left untouched.

`query()` also inserts a `rel=self` link per feature, which pygeoapi does not add on the items-list path. `get()` deliberately does **not**, because pygeoapi's `get_collection_item()` appends its own `rel=self` afterwards — adding one there would emit a duplicate.

## Configuration

Provider registration in `pygeoapi-config.yml`:

```yaml
providers:
  - type: feature
    name: swissgeo_provider.SwissGeoProvider
    data:
      host: ${POSTGRES_HOST:-localhost}
      port: ${POSTGRES_PORT:-5432}
      dbname: ${POSTGRES_DB:-swissgeo}
      user: ${POSTGRES_USER:-swissgeo}
      password: ${POSTGRES_PASSWORD:-swissgeo}
      search_path: [public]
    resource_id: swissgeo-features
    table: sample_features
    id_field: external_id
    geom_field: geom
    time_field: forecast_datetime
    title_field: point_name
    languages:
      - en
      - de
      - fr
      - it
```

Key environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PYGEOAPI_HOSTNAME` | `http://localhost:8080` | Base host used to identify same-host links for patching |
| `API_PREFIX` | `/api/oaf/rc1` | API path prefix |
| `POSTGRES_HOST` | `localhost` | PostGIS host |
| `POSTGRES_PORT` | `5432` | PostGIS port |
| `POSTGRES_DB` | `swissgeo` | PostGIS database name |
| `POSTGRES_USER` | `swissgeo` | PostGIS user |
| `POSTGRES_PASSWORD` | `swissgeo` | PostGIS password |
| `PYGEOAPI_CONFIG` | `/pygeoapi/pygeoapi-config.yml` | pygeoapi config file path |
| `DB_SEED_SAMPLE_DATA` | unset | When truthy, `scripts/init_database.py` seeds the local sample features |

## Running locally

```bash
docker compose up
```

This starts:
- **pygeoapi** on `http://localhost:8080/api/oaf/rc1/` (uvicorn, via `app.py`)
- **PostGIS** on port 5432, bootstrapped by the `init-database` service with a small sample dataset
- **OTel collector**, **Prometheus** (`:9090`) and **Jaeger** (`:16686`)

Copy `.env-docker` to configure environment variables before starting.

Try it:

```bash
curl 'http://localhost:8080/api/oaf/rc1/collections/swissgeo-features/items?lang=fr&f=json'
curl 'http://localhost:8080/api/oaf/rc1/collections/swissgeo-features/items?bbox=5.9,45.9,6.9,46.6&f=json'
```

## Database

The schema lives in `scripts/init_database.py`, which runs both locally (the `init-database`
compose service, before pygeoapi starts) and in kubernetes (as an init container, see
swissgeo/infra-kubernetes `services/service-oa-features/base/patch-init-database.yaml`).

It creates the owner role, the database, the PostGIS extension, the `sample_features` table and
its indexes. The role, database and extension steps are idempotent, but the table is **dropped and
recreated on every run**, so re-running it discards all existing rows — including on an ordinary
pod restart. The sample features are local-dev only and are inserted only when
`DB_SEED_SAMPLE_DATA` is truthy, which compose sets and the deployed environments do not.

The seed rows are read from the CSVs in `scripts/sample-data/` — a committed 3,360-row subset of
the MeteoSwiss OGD local-forecasting export (140 points × 24 hourly steps). See that directory's
README for what it contains and `scripts/make_sample_data.py` for how to rebuild it from a full
export.

To open a shell or re-apply the bootstrap against a running database:

```bash
just dbshell     # or: make dbshell
just reset-db    # or: make reset-db
```

## Debugging

Have ENV `PYDEBUG=true` set.

```bash
PYDEBUG=true docker compose up
```

This runs pygeoapi under [debugpy](https://github.com/microsoft/debugpy) listening on port 5678.

Then attach your debugger (e.g. **"Attach to Docker (swissgeo_provider)"** in Zed) to `localhost:5678`.

## Development

```bash
make setup   # create the virtualenv with uv
make test    # pytest with an HTML coverage report
make lint    # ruff + ty
make format  # ruff format
```

## Project structure

```
pygeoapi-swissgeo-extensions/
  app.py                  # Starlette entrypoint; patches call_api_threadsafe
  swissgeo_provider.py    # SwissGeoProvider: language selection + link patching
  otel.py                 # OpenTelemetry setup
  settings.py             # pydantic-settings configuration
pygeoapi-config.yml       # pygeoapi server + collection configuration
scripts/
  init_database.py        # DB/role/PostGIS/schema bootstrap + local seed data
  make_sample_data.py     # Dev tool: rebuilds sample-data/ from the full OGD export
  sample-data/            # Committed CSV subset the seeder reads
tests/                    # Provider unit tests
```

"""Synthetic check endpoint reporting whether the service and PostGIS are healthy.

The check verifies that the table of every ``SwissGeoProvider`` in the pygeoapi
configuration exists and that the database is reachable.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from opentelemetry import trace
from sqlalchemy import inspect
from starlette.requests import Request
from starlette.responses import JSONResponse

from swissgeo_oaf.provider.postgresql import SwissGeoProvider
from swissgeo_oaf.schemas.syntheticz import (
    Syntheticz,
    SyntheticzExternalSystem,
    SyntheticzServiceDetails,
    SyntheticzStatus,
)
from swissgeo_oaf.settings import get_settings
from swissgeo_oaf.version import __version__

LOGGER = logging.getLogger(__name__)

_tracer = trace.get_tracer(__name__)

_POSTGRESQL = "postgresql"

_PROVIDER_NAME = "swissgeo_oaf.provider.postgresql.SwissGeoProvider"


def _postgresql_providers(resources: dict) -> list[dict]:
    """Return the SwissGeoProvider definitions of the pygeoapi resources."""
    providers = [
        provider
        for resource in resources.values()
        for provider in resource.get("providers", [])
        if provider.get("name") == _PROVIDER_NAME
    ]
    if len(providers) == 0:
        raise ValueError("No PostgreSQL providers found")
    return providers


def _check_postgresql(providers: list[dict]) -> None:
    """Raise if one of the providers' tables is missing or PostGIS is unreachable."""
    for provider_def in providers:
        # Like pygeoapi on each request. The engine and the table model are cached per
        # process, so the provider only fails here until the table was found once; the
        # has_table() lookup below therefore queries the database on every check.
        provider = SwissGeoProvider(provider_def)
        # Set by pygeoapi's store_db_parameters(), so unknown to the type checker.
        search_path = getattr(provider, "db_search_path", ())
        schema = search_path[0] if search_path else None
        if not inspect(provider._engine).has_table(provider.table, schema=schema):  # noqa: SLF001
            raise LookupError(f"PostgreSQL table {provider.table} does not exist")


async def check(resources: dict) -> Syntheticz:
    settings = get_settings()
    syntheticz = Syntheticz(
        service=SyntheticzServiceDetails(name=settings.SERVICE_NAME, version=__version__),
        status=SyntheticzStatus.UP,
        external_systems={_POSTGRESQL: SyntheticzExternalSystem(status=SyntheticzStatus.UP)},
    )

    with _tracer.start_as_current_span("syntheticz-postgresql") as span:
        span.set_attribute("syntheticz", True)  # noqa: FBT003 ignore boolean-positional-value-in-call here
        try:
            # The database driver is blocking, so it runs in the default executor.
            await asyncio.to_thread(_check_postgresql, _postgresql_providers(resources))
        except Exception:
            # We explicitly use a broad exception here, as we want to be sure that any
            # exception is returning DOWN status, even if we don't know the exact reason.
            LOGGER.exception("Failed to access PostgreSQL")
            syntheticz.status = SyntheticzStatus.DOWN
            syntheticz.external_systems[_POSTGRESQL].status = SyntheticzStatus.DOWN

    return syntheticz


def syntheticz_endpoint(
    get_resources: Callable[[], dict],
) -> Callable[[Request], Awaitable[JSONResponse]]:
    """Return the synthetic check endpoint for the pygeoapi resources.

    *get_resources* is called on every check, as the discovery refresh
    (``swissgeo_oaf.refresh``) replaces the resources in the running app.
    """

    async def get_syntheticz(_request: Request) -> JSONResponse:
        health = await check(get_resources())
        return JSONResponse(
            health.model_dump(mode="json"),
            status_code=200 if health.status == SyntheticzStatus.UP else 500,
            headers={"Cache-Control": "no-store"},
        )

    return get_syntheticz

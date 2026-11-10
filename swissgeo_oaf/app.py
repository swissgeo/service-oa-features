"""
Starlette app entrypoint for uvicorn.

Patches call_api_threadsafe to work around a pygeoapi bug where
``request.raw_locale`` is a plain string. ``get_plugin_locale`` passes it
straight to ``l10n.best_match``, which only accepts a list or Locale and
silently drops anything else, so the provider locale always fell back to the
default (``en``) regardless of ``?lang=``. We rewrite ``_raw_locale`` to a
one-element list so the native locale-negotiation path resolves correctly.

Before pygeoapi is imported, recreates the sample table
(``swissgeo_oaf.sample_data``) and discovers the PostGIS tables to serve as
collections (``swissgeo_oaf.discovery``).

Also injects the ``lang``/``f`` query params into the executor thread via a
thread-local, used for same-host link patching in the provider.

Usage:
    uvicorn swissgeo_oaf.app:APP --host 0.0.0.0 --port 8080
"""

# ruff: noqa: E402 -- the startup work below has to run between the imports.
import asyncio
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from swissgeo_oaf.discovery import prepare_config
from swissgeo_oaf.sample_data import seed_sample_data
from swissgeo_oaf.settings import get_settings

settings = get_settings()

# The sample table behind the static sample-mch-param collection; recreated on
# every start, so it exists before pygeoapi first queries it.
if settings.sample_data_enabled:
    seed_sample_data(settings)

# pygeoapi reads its config file once, when pygeoapi.starlette_app is imported, so
# the PostGIS tables have to be discovered -- and PYGEOAPI_CONFIG pointed at the
# generated config -- before that import.
prepare_config()

import pygeoapi.starlette_app as _starlette_mod
from pygeoapi.api import API, APIRequest
from pygeoapi.starlette_app import APP as _PYGEOAPI_APP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.routing import Mount, Route

from swissgeo_oaf.cache_control import CacheControlMiddleware
from swissgeo_oaf.disable_html import DisableHtmlMiddleware
from swissgeo_oaf.otel import initialize_instrumentation, shutdown_otel
from swissgeo_oaf.provider.postgresql import set_request_params
from swissgeo_oaf.syntheticz import syntheticz_endpoint

_original_call_api_threadsafe = _starlette_mod.call_api_threadsafe


def _call_api_threadsafe_with_lang(  # pragma: no cover
    loop: asyncio.AbstractEventLoop,
    api_function: Callable,
    actual_api: API,
    api_request: APIRequest,
    *args: object,
) -> tuple:
    # pygeoapi stores raw_locale as a plain string; best_match needs a list.
    # Wrap it so get_plugin_locale resolves the requested language instead of
    # falling back to the default locale.
    raw_locale = api_request.raw_locale
    if isinstance(raw_locale, str):
        api_request._raw_locale = [raw_locale]  # noqa: SLF001

    set_request_params(
        lang=api_request.params.get("lang", None),
        fmt=api_request.params.get("f", None),
    )
    return _original_call_api_threadsafe(loop, api_function, actual_api, api_request, *args)


_starlette_mod.call_api_threadsafe = _call_api_threadsafe_with_lang  # ty: ignore[invalid-assignment]


async def _redirect_to_api(_request: Request) -> RedirectResponse:  # pragma: no cover
    return RedirectResponse(url=settings.api_prefix)


@asynccontextmanager  # pragma: no cover
async def _lifespan(_app: Starlette) -> AsyncGenerator[None]:
    # pygeoapi runs the API calls in the loop's default executor. Bound it
    # explicitly: the asyncio default derives from the node's CPU count, not the
    # container's limits.
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(
            max_workers=settings.threadpool_max_workers, thread_name_prefix="pygeoapi"
        ),
    )
    yield
    shutdown_otel()


APP = Starlette(
    routes=[
        Route("/", _redirect_to_api),
        # Must come before the pygeoapi mount, which would otherwise match the path.
        Route(
            f"{settings.api_prefix}/syntheticz",
            syntheticz_endpoint(_starlette_mod.CONFIG["resources"]),
        ),
        Mount(settings.api_prefix, app=_PYGEOAPI_APP),
    ],
    middleware=[
        Middleware(CacheControlMiddleware, value=settings.cache_control_header),
        *([] if settings.html_enabled else [Middleware(DisableHtmlMiddleware)]),
    ],
    lifespan=_lifespan,
)

initialize_instrumentation(APP)

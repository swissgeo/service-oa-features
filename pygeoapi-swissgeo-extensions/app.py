"""
Starlette app entrypoint for uvicorn.

Patches call_api_threadsafe to work around a pygeoapi bug where
``request.raw_locale`` is a plain string. ``get_plugin_locale`` passes it
straight to ``l10n.best_match``, which only accepts a list or Locale and
silently drops anything else, so the provider locale always fell back to the
default (``en``) regardless of ``?lang=``. We rewrite ``_raw_locale`` to a
one-element list so the native locale-negotiation path resolves correctly.

Also injects the ``lang``/``f`` query params into the executor thread via a
thread-local, used for same-host link patching in the provider.

Usage:
    uvicorn app:APP --host 0.0.0.0 --port 8080 --app-dir /pygeoapi/pygeoapi-swissgeo-extensions
"""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import pygeoapi.starlette_app as _starlette_mod
from otel import initialize_instrumentation, shutdown_otel
from pygeoapi.api import API, APIRequest
from pygeoapi.starlette_app import APP as _PYGEOAPI_APP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.routing import Mount, Route
from swissgeo_provider import set_request_params

_original_call_api_threadsafe = _starlette_mod.call_api_threadsafe


def _call_api_threadsafe_with_lang(
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


async def _redirect_to_api(_request: Request) -> RedirectResponse:
  return RedirectResponse(url="/api/oaf/rc1")


@asynccontextmanager
async def _lifespan(_app: Starlette) -> AsyncGenerator[None, None]:
  yield
  shutdown_otel()


APP = Starlette(
  routes=[
    Route("/", _redirect_to_api),
    Mount("/api/oaf/rc1", app=_PYGEOAPI_APP),
  ],
  lifespan=_lifespan,
)

initialize_instrumentation(APP)

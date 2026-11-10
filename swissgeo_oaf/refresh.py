"""Repeat the table discovery periodically and swap the new collections into pygeoapi.

``swissgeo_oaf.discovery`` runs once at startup, before pygeoapi reads its config.
pygeoapi reads ``api.config["resources"]`` on every request and serves
``api.openapi`` from memory, so collections can be changed in the running app by
replacing those on its ``API`` object (``pygeoapi.starlette_app.api_``).

Every interval, the refresher lists the tables the discovery rules match -- one
cheap query per rule. Only when that set differs from the last one does it run
the full discovery, regenerate the config and OpenAPI document, and swap them in.
A failed refresh is logged and leaves the served collections unchanged; the next
tick tries again.

Limitations:

- Only ``resources`` and the OpenAPI document are replaced. Other config changes
  (server, logging, ...) still need a restart.
- The fingerprint is the set of matched tables: a changed title in a table's
  ``<prefix>_<lang>`` columns is only picked up together with a table change, or
  after a restart.
- Each process refreshes on its own timer, so replicas can disagree on the
  collections for up to one interval.
"""

import asyncio
import copy
import logging
import os
from pathlib import Path

from pygeoapi.api import API
from pygeoapi.util import yaml_load

from swissgeo_oaf.discovery import generate, load_source, matched_tables

LOGGER = logging.getLogger(__name__)


class CollectionRefresher:
    """Keeps the discovered collections of a pygeoapi ``API`` up to date."""

    def __init__(self, source: Path, openapi_path: Path, interval_seconds: float) -> None:
        self.source = source
        self.openapi_path = openapi_path
        self.interval_seconds = interval_seconds
        self._tables: tuple[tuple[str, ...], ...] | None = None
        self._api: API | None = None

    @classmethod
    def from_environment(cls, interval_seconds: float) -> CollectionRefresher:
        """Create a refresher for the hand-written config named by ``PYGEOAPI_CONFIG``.

        Must be called before ``prepare_config()``, which points ``PYGEOAPI_CONFIG``
        at the generated file.
        """
        return cls(
            Path(os.environ["PYGEOAPI_CONFIG"]),
            Path(os.environ["PYGEOAPI_OPENAPI"]),
            interval_seconds,
        )

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0

    def snapshot(self) -> None:
        """Record the currently matched tables as the baseline.

        Call it *before* the startup discovery: a table created in between then
        differs from the baseline and is picked up on the first tick, rather than
        being missed.
        """
        self._tables = matched_tables(*load_source(self.source))

    def attach(self, api: API) -> None:
        """Set the pygeoapi ``API`` whose collections are replaced."""
        self._api = api

    def refresh(self) -> bool:
        """Rediscover the tables; swap in the new collections if they changed.

        Blocking (database and file I/O); run it off the event loop. Returns whether
        the collections were replaced.
        """
        if self._api is None:
            raise RuntimeError("CollectionRefresher.refresh() called before attach()")
        raw_config, config = load_source(self.source)
        tables = matched_tables(raw_config, config)
        if tables == self._tables:
            LOGGER.debug("discovery refresh: tables unchanged")
            return False

        target = generate(self.source, self.openapi_path, raw_config, config)
        with target.open(encoding="utf-8") as handle:
            resources = yaml_load(handle)["resources"]
        openapi = self._load_openapi()

        # Replace (rather than mutate) each object, so requests already running
        # keep a consistent view of the old one.
        api = self._api
        api.config["resources"] = resources
        api.tpl_config["resources"] = copy.deepcopy(resources)
        # A str for a JSON document, as from pygeoapi's own load_openapi_document().
        api.openapi = openapi  # ty: ignore[invalid-assignment]
        self._tables = tables
        LOGGER.info("discovery refresh: now serving %d collections", len(resources))
        return True

    def _load_openapi(self) -> dict | str:
        """Read the OpenAPI document like pygeoapi's ``load_openapi_document()``."""
        with self.openapi_path.open(encoding="utf-8") as handle:
            if self.openapi_path.suffix in (".yaml", ".yml"):
                return yaml_load(handle)
            return handle.read()

    async def run(self) -> None:
        """Refresh every ``interval_seconds`` until cancelled."""
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await asyncio.to_thread(self.refresh)
            except Exception:
                # Broad on purpose: keep serving the current collections and retry
                # on the next tick, whatever went wrong (database down, bad table…).
                LOGGER.exception("discovery refresh failed, keeping the current collections")

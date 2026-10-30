"""SwissGeo PostGIS feature provider for OGC API Features.

Extends PostgreSQLProvider with language-aware field selection:
``parameter_description``, ``parameter_group`` and ``point_type_name`` arrive as
per-language JSONB objects (``{"de": …, "fr": …}``) and are collapsed to the
language pygeoapi resolves (passed in via the ``language`` kwarg) using
``pygeoapi.l10n.translate``, before handing results back to pygeoapi.

Also patches same-host links to carry ``lang`` and ``f`` query params.

Usage in pygeoapi-config.yml:
    providers:
      - type: feature
        name: swissgeo_provider.SwissGeoProvider
        data:
          host: postgres
          port: 5432
          dbname: swissgeo
          user: swissgeo
          password: swissgeo
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
"""

import logging
import os
import threading
from urllib.parse import urlencode, urlparse

from opentelemetry import trace
from pygeoapi import l10n
from pygeoapi.provider.sql import PostgreSQLProvider

LOGGER = logging.getLogger(__name__)

_tracer = trace.get_tracer(__name__)

_SUPPORTED_LANGS = {"de", "en", "fr", "it"}

# Columns stored as per-language JSONB objects, collapsed by _translate_props().
# Must stay in sync with the table definition in scripts/init_database.py.
#
# point_name is deliberately absent: the MeteoSwiss source ships one name per
# point rather than a per-language struct, so it is plain TEXT and passes
# through untranslated. point_type_name carries the localised label instead.
_LANG_STRUCT_FIELDS = ("parameter_description", "parameter_group", "point_type_name")

_local = threading.local()


def set_request_params(
  lang: str | None,
  fmt: str | None,
) -> None:
  """Set lang, fmt on the current thread-local before an API call."""
  _local.lang = lang
  _local.fmt = fmt


def _get_lang_and_fmt() -> tuple[str, str | None]:
  """Read lang and fmt from thread-local set by app.py."""
  lang = getattr(_local, "lang", None)
  fmt = getattr(_local, "fmt", None)
  if not lang:
    return "en", fmt
  primary = lang.split("-")[0].split("_")[0].lower()
  return (primary if primary in _SUPPORTED_LANGS else "en"), fmt


def _get_base_url() -> str:
  """Return the server base URL set by app.py, or fall back to env vars."""
  return f"{os.environ.get('PYGEOAPI_HOSTNAME', 'http://localhost:8080')}{os.environ.get('API_PREFIX', '/api/oaf/rc1')}"


class SwissGeoProvider(PostgreSQLProvider):
  """OGC API Features provider backed by PostGIS.

  Adds language-aware JSONB language-struct collapsing and same-host
  link patching on top of the standard PostgreSQLProvider.
  """

  @_tracer.start_as_current_span("SwissGeoProvider.__init__")
  def __init__(self, provider_def: dict) -> None:
    LOGGER.info("SwissGeoProvider.__init__ called")
    super().__init__(provider_def)
    self.resource_id = provider_def.get("resource_id", self.name)

  @_tracer.start_as_current_span("SwissGeoProvider.query")
  def query(  # noqa: ANN201, PLR0913, PLR0917
    self,
    offset: int = 0,
    limit: int = 10,
    resulttype: str = "results",
    bbox: list | None = None,
    datetime_: str | None = None,
    properties: list | None = None,
    sortby: list | None = None,
    select_properties: list | None = None,
    skip_geometry: bool = False,
    q: str | None = None,
    filterq: str | None = None,
    crs_transform_spec: object | None = None,
    **kwargs,
  ):
    """Execute a feature query with language-aware post-processing."""
    if select_properties is None:
      select_properties = []
    if sortby is None:
      sortby = []
    if properties is None:
      properties = []
    if bbox is None:
      bbox = []
    language = kwargs.get("language")
    lang, fmt = _get_lang_and_fmt()
    LOGGER.debug("SwissGeoProvider.query language=%s fmt=%s", language, fmt)

    result = super().query(
      offset=offset,
      limit=limit,
      resulttype=resulttype,
      bbox=bbox,
      datetime_=datetime_,
      properties=properties,
      sortby=sortby,
      select_properties=select_properties,
      skip_geometry=skip_geometry,
      q=q,
      filterq=filterq,
      crs_transform_spec=crs_transform_spec,
      **kwargs,
    )

    for feature in result.get("features", []):
      _translate_props(feature.get("properties", {}), language)
      links = feature.setdefault("links", [])
      _ensure_self_link(links, self.resource_id, feature.get("id", ""))
      _patch_links(links, lang, fmt)

    return result

  @_tracer.start_as_current_span("SwissGeoProvider.get")
  def get(self, identifier: str, crs_transform_spec: object | None = None, **kwargs) -> dict | None:
    """Fetch a single feature by ID with language-aware post-processing."""
    language = kwargs.get("language")
    lang, fmt = _get_lang_and_fmt()
    LOGGER.debug(
      "SwissGeoProvider.get identifier=%s language=%s fmt=%s",
      identifier,
      language,
      fmt,
    )

    result = super().get(identifier, crs_transform_spec=crs_transform_spec, **kwargs)

    if result:
      _translate_props(result.get("properties", {}), language)
      # No _ensure_self_link() here: pygeoapi's get_collection_item() always
      # appends its own rel=self link to the item afterwards, so adding one
      # at provider level would emit a duplicate. The items-list path does
      # not do that, which is why query() still calls _ensure_self_link().
      links = result.setdefault("links", [])
      _patch_links(links, lang, fmt)

    return result


def _translate_props(props: dict, language) -> None:  # noqa: ANN001
  """Collapse the JSONB language structs in place.

  Uses pygeoapi's own :func:`pygeoapi.l10n.translate` so behaviour matches
  the rest of the framework: the value for *language* is returned, falling
  back to the first available language, then to the struct itself. Only the
  known language-struct fields are translated to avoid l10n warnings on
  non-locale sibling keys (``type``, ``rel``, …).
  """
  if not language:
    return
  for field in _LANG_STRUCT_FIELDS:
    if isinstance(props.get(field), dict):
      props[field] = l10n.translate(props[field], language)


def _ensure_self_link(links: list, collection_id: str, item_id: str) -> None:
  """Insert a ``rel=self`` link if none is present in *links*."""
  if any(link.get("rel") == "self" for link in links):
    return
  if not item_id:
    return
  base_url = _get_base_url()
  href = f"/collections/{collection_id}/items/{item_id}"
  if base_url:
    href = f"{base_url}{href}"
  links.insert(
    0,
    {
      "href": href,
      "rel": "self",
      "type": "application/geo+json",
    },
  )


def _patch_links(links: list, lang: str, fmt: str | None) -> None:
  """Append ``lang`` (and ``f`` if present) to same-host and relative links.

  External links are left untouched.
  """
  params: dict[str, str] = {"lang": lang}
  if fmt:
    params["f"] = fmt
  qs = urlencode(params)
  base_url = _get_base_url()

  for link in links:
    href = link.get("href", "")
    if not href:
      continue
    parsed = urlparse(href)
    is_relative = not parsed.scheme
    is_same_host = base_url and href.startswith(base_url)
    if is_relative or is_same_host:
      if is_relative and base_url:
        href = f"{base_url}{href}"
      sep = "&" if "?" in href else "?"
      link["href"] = f"{href}{sep}{qs}"

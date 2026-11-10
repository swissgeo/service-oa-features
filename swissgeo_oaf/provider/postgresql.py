"""SwissGeo PostGIS feature provider for OGC API Features.

Extends PostgreSQLProvider with language-aware field selection:
``parameter_description``, ``parameter_group`` and ``point_type`` are stored as
one column per language (``point_type_de``, ``point_type_fr``, …) and are
collapsed into a single property holding the language pygeoapi resolves (passed
in via the ``language`` kwarg), using ``pygeoapi.l10n.translate``, before
handing results back to pygeoapi.

Also patches same-host links to carry ``lang`` and ``f`` query params.

Usage in pygeoapi-config.yml:
    providers:
      - type: feature
        name: swissgeo_oaf.provider.postgresql.SwissGeoProvider
        data:
          host: postgres
          port: 5432
          dbname: swissgeo
          user: swissgeo
          password: swissgeo
        table: tbl_ogd-local-forecasting-tre200px
        id_field: id
        geom_field: geometry
        time_field: datetime
        title_field: point_name
        languages:
          - en
          - de
          - fr
          - it
"""

import logging
import threading
from urllib.parse import urlencode, urlparse

from opentelemetry import trace
from pygeoapi import l10n
from pygeoapi.provider.sql import PostgreSQLProvider

from swissgeo_oaf.settings import get_settings

LOGGER = logging.getLogger(__name__)

_tracer = trace.get_tracer(__name__)

_SUPPORTED_LANGS = {"de", "en", "fr", "it"}

# Order of the per-language columns handed to l10n.translate, which falls back to
# the first one present when the requested language has no column -- so en first.
_LANG_COLUMN_ORDER = ("en", "de", "fr", "it")

# Properties stored as one `<field>_<lang>` column per language, collapsed into a
# single `<field>` property by _translate_props(). Must stay in sync with the
# MeteoSwiss table definition (see swissgeo_oaf/sample_data.py).
#
# point_name is deliberately absent: the MeteoSwiss source ships one name per
# point, so it passes through untranslated. point_type carries the localised
# label instead.
_LANG_FIELDS = ("parameter_description", "parameter_group", "point_type")

# `<field>_<lang>` column name -> (field, lang).
_LANG_COLUMNS = {
    f"{field}_{lang}": (field, lang) for field in _LANG_FIELDS for lang in _LANG_COLUMN_ORDER
}

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
    """Return the public base URL of the API, from the settings."""
    settings = get_settings()
    return f"{settings.pygeoapi_hostname}{settings.api_prefix}"


class SwissGeoProvider(PostgreSQLProvider):
    """OGC API Features provider backed by PostGIS.

    Adds language-aware collapsing of the per-language columns and same-host
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
        if select_properties is None:  # pragma: no branch
            select_properties = []
        if sortby is None:  # pragma: no branch
            sortby = []
        if properties is None:  # pragma: no branch
            properties = []
        if bbox is None:  # pragma: no branch
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
    def get(
        self, identifier: str, crs_transform_spec: object | None = None, **kwargs
    ) -> dict | None:
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
    """Collapse the per-language column families in place.

    ``point_type_de`` / ``_fr`` / ``_it`` / ``_en`` become one ``point_type``
    property, placed where the first of its columns was. Same for
    ``parameter_description`` and ``parameter_group``.

    Uses pygeoapi's own :func:`pygeoapi.l10n.translate` so behaviour matches
    the rest of the framework: the value for *language* is returned, falling
    back to the first column present (en, de, fr, it in that order). Without a
    *language* the columns are left as they are.
    """
    if not language:
        return

    # Untyped on purpose: l10n.translate() is annotated `value: str` although it is
    # documented (and used here) to take a language struct.
    families: dict = {}
    for column, (field, lang) in _LANG_COLUMNS.items():
        if column in props:
            families.setdefault(field, {})[lang] = props[column]
    if not families:
        return

    collapsed: dict = {}
    for key, value in props.items():
        if key not in _LANG_COLUMNS:
            collapsed[key] = value
            continue
        field = _LANG_COLUMNS[key][0]
        if field not in collapsed:
            collapsed[field] = l10n.translate(families[field], language)
    props.clear()
    props.update(collapsed)


def _ensure_self_link(links: list, collection_id: str, item_id: str) -> None:
    """Insert a ``rel=self`` link if none is present in *links*."""
    if any(link.get("rel") == "self" for link in links):
        return
    if not item_id:
        return
    base_url = _get_base_url()
    href = f"/collections/{collection_id}/items/{item_id}"
    if base_url:  # pragma: no branch
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

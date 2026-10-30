"""Tests for swissgeo_provider helper functions."""

import sys
import threading
from pathlib import Path

from babel import Locale

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "pygeoapi-swissgeo-extensions"))

import swissgeo_provider
from swissgeo_provider import (
  SwissGeoProvider,
  _ensure_self_link,
  _get_base_url,
  _get_lang_and_fmt,
  _local,
  _patch_links,
  _translate_props,
  set_request_params,
)

# ---------------------------------------------------------------------------
# set_request_params / _get_lang_and_fmt
# ---------------------------------------------------------------------------


class TestGetLangAndFmt:
  def setup_method(self) -> None:
    # Clear thread-local state before each test
    _local.__dict__.clear()

  def test_defaults_to_en_when_no_lang(self) -> None:
    lang, fmt = _get_lang_and_fmt()
    assert lang == "en"
    assert fmt is None

  def test_supported_lang_returned_as_is(self) -> None:
    set_request_params(lang="de", fmt=None)
    lang, _fmt = _get_lang_and_fmt()
    assert lang == "de"

  @pytest.mark.parametrize("code", ["de", "fr", "it", "en"])
  def test_all_supported_langs(self, code) -> None:
    set_request_params(lang=code, fmt=None)
    lang, _ = _get_lang_and_fmt()
    assert lang == code

  def test_unsupported_lang_falls_back_to_en(self) -> None:
    set_request_params(lang="es", fmt=None)
    lang, _ = _get_lang_and_fmt()
    assert lang == "en"

  def test_locale_tag_stripped_to_primary(self) -> None:
    set_request_params(lang="de-CH", fmt=None)
    lang, _ = _get_lang_and_fmt()
    assert lang == "de"

  def test_underscore_locale_stripped(self) -> None:
    set_request_params(lang="fr_CH", fmt=None)
    lang, _ = _get_lang_and_fmt()
    assert lang == "fr"

  def test_fmt_propagated(self) -> None:
    set_request_params(lang="en", fmt="json")
    _, fmt = _get_lang_and_fmt()
    assert fmt == "json"

  def test_thread_isolation(self) -> None:
    """Each thread gets its own lang/fmt."""
    results = {}

    def run(name, lang) -> None:
      set_request_params(lang=lang, fmt=None)
      results[name] = _get_lang_and_fmt()[0]

    t1 = threading.Thread(target=run, args=("a", "de"))
    t2 = threading.Thread(target=run, args=("b", "fr"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["a"] == "de"
    assert results["b"] == "fr"


# ---------------------------------------------------------------------------
# _translate_props
# ---------------------------------------------------------------------------


class TestTranslateProps:
  def test_point_type_name_collapsed_to_requested_lang(self) -> None:
    props = {"point_type_name": {"de": "Zentrum Postleitzahl", "fr": "Centre code postal"}}
    _translate_props(props, "de")
    assert props["point_type_name"] == "Zentrum Postleitzahl"

  def test_point_name_never_translated(self) -> None:
    """point_name is plain TEXT in the source data, so it must pass through.

    The MeteoSwiss export ships a single name per point rather than a
    per-language struct; only point_type carries localised labels.
    """
    props = {"point_name": "Delémont"}
    _translate_props(props, "de")
    assert props["point_name"] == "Delémont"

  def test_parameter_description_collapsed_to_requested_lang(self) -> None:
    props = {"parameter_description": {"de": "Deutsch", "fr": "Français"}}
    _translate_props(props, "fr")
    assert props["parameter_description"] == "Français"

  def test_parameter_group_collapsed_to_requested_lang(self) -> None:
    props = {"parameter_group": {"de": "Temperatur", "fr": "Température"}}
    _translate_props(props, "fr")
    assert props["parameter_group"] == "Température"

  def test_all_lang_struct_fields_collapsed_together(self) -> None:
    props = {
      "parameter_description": {"de": "Temperatur", "en": "Temperature"},
      "parameter_group": {"de": "Temperatur", "en": "Temperature"},
      "point_type_name": {"de": "Station", "en": "Station"},
      "parameter_shortname": "tre200h0",
      "point_name": "Genève",
      "station_abbr": "GVE",
      "value": 3.1,
    }
    _translate_props(props, "de")
    assert props["parameter_description"] == "Temperatur"
    assert props["parameter_group"] == "Temperatur"
    assert props["point_type_name"] == "Station"
    # Non-struct columns pass through untouched.
    assert props["parameter_shortname"] == "tre200h0"
    assert props["point_name"] == "Genève"
    assert props["station_abbr"] == "GVE"
    assert props["value"] == 3.1

  def test_accepts_babel_locale(self) -> None:
    locale = Locale("de")
    props = {"point_type_name": {"de": "Deutsch", "fr": "Français"}}
    _translate_props(props, locale)
    assert props["point_type_name"] == "Deutsch"

  def test_falls_back_to_first_lang_when_requested_missing(self) -> None:
    props = {"point_type_name": {"de": "Deutsch"}}
    _translate_props(props, "it")
    assert props["point_type_name"] == "Deutsch"

  def test_no_language_leaves_struct_untouched(self) -> None:
    props = {"point_type_name": {"de": "Deutsch", "fr": "Français"}}
    _translate_props(props, None)
    assert props["point_type_name"] == {"de": "Deutsch", "fr": "Français"}

  def test_non_dict_field_untouched(self) -> None:
    props = {"point_type_name": "plain string"}
    _translate_props(props, "de")
    assert props["point_type_name"] == "plain string"

  def test_missing_field_ignored(self) -> None:
    props = {"parameter_description": {"de": "Deutsch"}}
    _translate_props(props, "de")
    assert "point_type_name" not in props
    assert props["parameter_description"] == "Deutsch"

  def test_non_lang_fields_untouched(self) -> None:
    props = {"point_type_name": {"en": "T"}, "extra": "keep me"}
    _translate_props(props, "en")
    assert props["extra"] == "keep me"


# ---------------------------------------------------------------------------
# _ensure_self_link
# ---------------------------------------------------------------------------


class TestEnsureSelfLink:
  def setup_method(self) -> None:
    _local.__dict__.clear()

  def test_inserts_self_link_when_absent(self) -> None:
    links: list = []
    _ensure_self_link(links, "my-collection", "item-1")
    assert len(links) == 1
    assert links[0]["rel"] == "self"
    assert "my-collection/items/item-1" in links[0]["href"]

  def test_does_not_duplicate_self_link(self) -> None:
    links = [{"rel": "self", "href": "http://example.com/existing"}]
    _ensure_self_link(links, "my-collection", "item-1")
    assert len(links) == 1

  def test_skips_when_item_id_empty(self) -> None:
    links: list = []
    _ensure_self_link(links, "my-collection", "")
    assert links == []

  def test_prepends_server_url_when_available(self, monkeypatch) -> None:
    monkeypatch.setenv("PYGEOAPI_HOSTNAME", "https://api.example.com")
    monkeypatch.setenv("API_PREFIX", "/")

    set_request_params(lang=None, fmt=None)
    links: list = []
    _ensure_self_link(links, "col", "abc")
    assert links[0]["href"].startswith("https://api.example.com")

  def test_self_link_type_is_geojson(self) -> None:
    links: list = []
    _ensure_self_link(links, "col", "xyz")
    assert links[0]["type"] == "application/geo+json"


# ---------------------------------------------------------------------------
# _patch_links
# ---------------------------------------------------------------------------


class TestPatchLinks:
  def setup_method(self) -> None:
    _local.__dict__.clear()

  def test_appends_lang_to_relative_link(self) -> None:
    links = [{"href": "/collections/col/items/1"}]
    _patch_links(links, "de", None)
    assert "lang=de" in links[0]["href"]

  def test_appends_fmt_when_provided(self) -> None:
    links = [{"href": "/collections/col/items/1"}]
    _patch_links(links, "fr", "json")
    assert "f=json" in links[0]["href"]

  def test_no_fmt_param_when_fmt_is_none(self) -> None:
    links = [{"href": "/collections/col/items/1"}]
    _patch_links(links, "en", None)
    assert "f=" not in links[0]["href"]

  def test_does_not_patch_external_links(self) -> None:
    links = [{"href": "https://external.example.com/resource"}]
    _patch_links(links, "de", None)
    assert "lang=" not in links[0]["href"]

  def test_patches_same_host_link(self, monkeypatch) -> None:
    monkeypatch.setenv("PYGEOAPI_HOSTNAME", "https://api.example.com")
    monkeypatch.setenv("API_PREFIX", "/")

    set_request_params(lang=None, fmt=None)
    links = [{"href": "https://api.example.com/collections/col/items/1"}]
    _patch_links(links, "it", None)
    assert "lang=it" in links[0]["href"]

  def test_uses_ampersand_when_query_string_already_present(self) -> None:
    links = [{"href": "/items/1?f=json"}]
    _patch_links(links, "de", None)
    href = links[0]["href"]
    assert href.count("?") == 1
    assert "&lang=de" in href

  def test_skips_link_with_empty_href(self) -> None:
    links = [{"href": ""}]
    _patch_links(links, "de", None)
    assert links[0]["href"] == ""

  def test_prepends_server_url_to_relative_link(self, monkeypatch) -> None:
    monkeypatch.setenv("PYGEOAPI_HOSTNAME", "https://api.example.com")
    monkeypatch.setenv("API_PREFIX", "")

    set_request_params(lang=None, fmt=None)
    links = [{"href": "/collections/col"}]
    _patch_links(links, "en", None)
    assert links[0]["href"].startswith("https://api.example.com/collections/col")

  def test_patches_all_links_in_list(self) -> None:
    links = [
      {"href": "/a"},
      {"href": "/b"},
    ]
    _patch_links(links, "de", None)
    assert "lang=de" in links[0]["href"]
    assert "lang=de" in links[1]["href"]

  def test_empty_link_list_is_noop(self) -> None:
    links: list = []
    _patch_links(links, "de", None)
    assert links == []

  def test_link_without_href_key_skipped(self) -> None:
    links = [{"rel": "self"}]
    _patch_links(links, "de", None)
    assert "href" not in links[0]

  def test_relative_link_without_base_url_left_relative(self, monkeypatch) -> None:
    monkeypatch.setenv("PYGEOAPI_HOSTNAME", "")
    monkeypatch.setenv("API_PREFIX", "")
    set_request_params(lang=None, fmt=None)
    links = [{"href": "/collections/col"}]
    _patch_links(links, "de", None)
    # Still patched (relative), but no host prepended.
    assert links[0]["href"].startswith("/collections/col")
    assert "lang=de" in links[0]["href"]

  def test_both_lang_and_fmt_appended(self) -> None:
    links = [{"href": "/items/1"}]
    _patch_links(links, "fr", "html")
    href = links[0]["href"]
    assert "lang=fr" in href
    assert "f=html" in href


# ---------------------------------------------------------------------------
# _get_base_url
# ---------------------------------------------------------------------------


class TestGetBaseUrl:
  def test_defaults_when_env_unset(self, monkeypatch) -> None:
    monkeypatch.delenv("PYGEOAPI_HOSTNAME", raising=False)
    monkeypatch.delenv("API_PREFIX", raising=False)
    assert _get_base_url() == "http://localhost:8080/api/oaf/rc1"

  def test_uses_env_vars(self, monkeypatch) -> None:
    monkeypatch.setenv("PYGEOAPI_HOSTNAME", "https://api.example.com")
    monkeypatch.setenv("API_PREFIX", "/prefix")
    assert _get_base_url() == "https://api.example.com/prefix"

  def test_empty_prefix(self, monkeypatch) -> None:
    monkeypatch.setenv("PYGEOAPI_HOSTNAME", "https://api.example.com")
    monkeypatch.setenv("API_PREFIX", "")
    assert _get_base_url() == "https://api.example.com"


# ---------------------------------------------------------------------------
# SwissGeoProvider.__init__
# ---------------------------------------------------------------------------


class TestProviderInit:
  def _stub_parent_init(self, monkeypatch) -> None:
    """Stub the parent __init__ so it sets ``name`` without a database."""

    def fake_init(self, provider_def) -> None:
      self.name = provider_def.get("name", "swissgeo-features")

    monkeypatch.setattr(
      swissgeo_provider.PostgreSQLProvider,
      "__init__",
      fake_init,
    )

  def test_resource_id_defaults_to_name(self, monkeypatch) -> None:
    self._stub_parent_init(monkeypatch)
    provider = SwissGeoProvider({"name": "my-catalog"})
    assert provider.resource_id == "my-catalog"

  def test_resource_id_from_provider_def(self, monkeypatch) -> None:
    self._stub_parent_init(monkeypatch)
    provider = SwissGeoProvider({"name": "my-catalog", "resource_id": "explicit"})
    assert provider.resource_id == "explicit"


# ---------------------------------------------------------------------------
# SwissGeoProvider.query / .get
#
# The parent PostgreSQLProvider needs a live PostGIS database, so
# tests build the instance without running __init__ and stub the parent's
# query/get to return canned PostGIS-shaped results. This exercises the
# language-aware post-processing that SwissGeoProvider layers on top.
# ---------------------------------------------------------------------------


def _make_provider(resource_id="col") -> SwissGeoProvider:
  """Build a SwissGeoProvider without touching the real parent __init__."""
  provider = object.__new__(SwissGeoProvider)
  provider.resource_id = resource_id
  return provider


class TestProviderQuery:
  def setup_method(self) -> None:
    _local.__dict__.clear()

  def test_translates_feature_props_and_adds_links(self, monkeypatch) -> None:
    provider = _make_provider("col")
    parent_result = {
      "features": [
        {
          "id": "rec-1",
          "properties": {"point_type_name": {"de": "Titel", "fr": "Titre"}},
        },
      ],
    }
    monkeypatch.setattr(
      swissgeo_provider.PostgreSQLProvider,
      "query",
      lambda _self, **_kwargs: parent_result,
    )
    set_request_params(lang="de", fmt=None)

    result = provider.query(language="de")

    feature = result["features"][0]
    assert feature["properties"]["point_type_name"] == "Titel"
    self_links = [link for link in feature["links"] if link["rel"] == "self"]
    assert len(self_links) == 1
    assert "col/items/rec-1" in self_links[0]["href"]
    assert all("lang=de" in link["href"] for link in feature["links"])

  def test_geometry_left_untouched(self, monkeypatch) -> None:
    """PostGIS geometry passes through the post-processing unchanged."""
    geometry = {"type": "Point", "coordinates": [7.4474, 46.948]}
    provider = _make_provider("col")
    parent_result = {
      "features": [
        {
          "id": "rec-1",
          "geometry": geometry,
          "properties": {"point_type_name": {"de": "Bern"}},
        },
      ],
    }
    monkeypatch.setattr(
      swissgeo_provider.PostgreSQLProvider,
      "query",
      lambda _self, **_kwargs: parent_result,
    )
    set_request_params(lang="de", fmt=None)

    result = provider.query(language="de")

    assert result["features"][0]["geometry"] == geometry

  def test_existing_links_are_patched_not_replaced(self, monkeypatch) -> None:
    provider = _make_provider("col")
    parent_result = {
      "features": [
        {
          "id": "rec-1",
          "properties": {},
          "links": [{"rel": "alternate", "href": "/collections/col/items/rec-1"}],
        },
      ],
    }
    monkeypatch.setattr(
      swissgeo_provider.PostgreSQLProvider,
      "query",
      lambda _self, **_kwargs: parent_result,
    )
    set_request_params(lang="fr", fmt="json")

    result = provider.query(language="fr")

    links = result["features"][0]["links"]
    rels = [link["rel"] for link in links]
    assert "alternate" in rels
    assert "self" in rels
    alternate = next(link for link in links if link["rel"] == "alternate")
    assert "lang=fr" in alternate["href"]
    assert "f=json" in alternate["href"]

  def test_empty_result_returned_unchanged(self, monkeypatch) -> None:
    provider = _make_provider("col")
    monkeypatch.setattr(
      swissgeo_provider.PostgreSQLProvider,
      "query",
      lambda _self, **_kwargs: {"features": []},
    )
    set_request_params(lang="en", fmt=None)

    result = provider.query()

    assert result == {"features": []}

  def test_none_kwargs_normalised_before_parent(self, monkeypatch) -> None:
    """query() passes lists (not None) for the collection args to the parent."""
    captured = {}

    def fake_query(_self, **kwargs) -> dict:
      captured.update(kwargs)
      return {"features": []}

    provider = _make_provider("col")
    monkeypatch.setattr(
      swissgeo_provider.PostgreSQLProvider,
      "query",
      fake_query,
    )
    set_request_params(lang="en", fmt=None)

    provider.query()

    assert captured["select_properties"] == []
    assert captured["sortby"] == []
    assert captured["properties"] == []
    assert captured["bbox"] == []


class TestProviderGet:
  def setup_method(self) -> None:
    _local.__dict__.clear()

  def test_translates_props(self, monkeypatch) -> None:
    provider = _make_provider("col")
    parent_result = {
      "id": "rec-1",
      "properties": {"parameter_description": {"de": "Beschreibung", "en": "Description"}},
    }
    monkeypatch.setattr(
      swissgeo_provider.PostgreSQLProvider,
      "get",
      lambda _self, _identifier, **_kwargs: parent_result,
    )
    set_request_params(lang="de", fmt=None)

    result = provider.get("rec-1", language="de")

    assert result is not None
    assert result["properties"]["parameter_description"] == "Beschreibung"

  def test_does_not_add_self_link(self, monkeypatch) -> None:
    """pygeoapi's get_collection_item() adds its own rel=self afterwards."""
    provider = _make_provider("col")
    parent_result = {"id": "rec-1", "properties": {}}
    monkeypatch.setattr(
      swissgeo_provider.PostgreSQLProvider,
      "get",
      lambda _self, _identifier, **_kwargs: parent_result,
    )
    set_request_params(lang="de", fmt=None)

    result = provider.get("rec-1", language="de")

    assert result is not None
    assert [link for link in result["links"] if link.get("rel") == "self"] == []

  def test_patches_existing_links(self, monkeypatch) -> None:
    provider = _make_provider("col")
    parent_result = {
      "id": "rec-1",
      "properties": {},
      "links": [{"rel": "alternate", "href": "/collections/col/items/rec-1"}],
    }
    monkeypatch.setattr(
      swissgeo_provider.PostgreSQLProvider,
      "get",
      lambda _self, _identifier, **_kwargs: parent_result,
    )
    set_request_params(lang="it", fmt=None)

    result = provider.get("rec-1", language="it")

    assert result is not None
    assert "lang=it" in result["links"][0]["href"]

  def test_none_result_returned_as_is(self, monkeypatch) -> None:
    provider = _make_provider("col")
    monkeypatch.setattr(
      swissgeo_provider.PostgreSQLProvider,
      "get",
      lambda _self, _identifier, **_kwargs: None,
    )
    set_request_params(lang="en", fmt=None)

    assert provider.get("missing") is None

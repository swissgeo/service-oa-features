"""Tests for the settings."""

import re
from pathlib import Path

import pytest

from swissgeo_oaf.settings import Settings

_PYGEOAPI_CONFIG = Path(__file__).parent.parent / "pygeoapi-config.yml"


@pytest.mark.parametrize(
    ("env_var", "field"),
    [("PYGEOAPI_HOSTNAME", "pygeoapi_hostname"), ("API_PREFIX", "api_prefix")],
)
def test_pygeoapi_config_defaults_match_settings(env_var: str, field: str) -> None:
    # pygeoapi reads server.url from its configuration file itself, so the fallback
    # values there have to be kept in sync with the settings defaults.
    match = re.search(rf"\$\{{{env_var}:-([^}}]*)\}}", _PYGEOAPI_CONFIG.read_text())
    assert match, f"${{{env_var}:-...}} not found in {_PYGEOAPI_CONFIG.name}"
    assert match.group(1) == Settings.model_fields[field].default

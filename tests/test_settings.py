"""Tests for the settings."""

import re
from pathlib import Path

import pytest

from swissgeo_oaf.settings import Settings

_PYGEOAPI_CONFIG = Path(__file__).parent.parent / "pygeoapi-config.yml"


@pytest.mark.parametrize(
    ("env_var", "field"),
    [
        ("PYGEOAPI_HOSTNAME", "pygeoapi_hostname"),
        ("API_PREFIX", "api_prefix"),
        # The sample table is seeded with the settings, the collections read it with
        # the provider settings from the config file: both must reach the same DB.
        ("DB_HOST", "db_host"),
        ("DB_PORT", "db_port"),
        ("DB_NAME", "db_name"),
        ("DB_USER", "db_user"),
        ("DB_PW", "db_pw"),
        ("DB_SSLMODE", "db_sslmode"),
    ],
)
def test_pygeoapi_config_defaults_match_settings(env_var: str, field: str) -> None:
    # pygeoapi reads its configuration file itself, so the fallback values there
    # have to be kept in sync with the settings defaults -- in every occurrence.
    matches = re.findall(rf"\$\{{{env_var}:-([^}}]*)\}}", _PYGEOAPI_CONFIG.read_text())
    assert matches, f"${{{env_var}:-...}} not found in {_PYGEOAPI_CONFIG.name}"
    assert set(matches) == {str(Settings.model_fields[field].default)}

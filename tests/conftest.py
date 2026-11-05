from collections.abc import Iterator

import pytest

from swissgeo_oaf.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Re-read the settings in each test, so that environment changes apply."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

"""Home Assistant fixtures for integration regression tests."""

import pytest_asyncio
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame


@pytest_asyncio.fixture
async def hass(tmp_path):
    """Provide a local Home Assistant instance without hardware or network setup."""
    instance = HomeAssistant(str(tmp_path))
    frame.async_setup(instance)
    yield instance
    await instance.async_stop(force=True)

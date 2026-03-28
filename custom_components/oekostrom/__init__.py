"""The oekostrom AG integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import OekostromApi, OekostromApiError, OekostromAuthError
from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import OekostromCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up oekostrom AG from a config entry."""
    api = OekostromApi(
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )

    try:
        await api.authenticate()
    except OekostromAuthError as err:
        await api.close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except OekostromApiError as err:
        await api.close()
        raise ConfigEntryNotReady(
            f"Cannot connect to oekostrom portal: {err}"
        ) from err

    coordinator = OekostromCoordinator(hass, api)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await api.close()
        raise

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: OekostromCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.api.close()
    return unload_ok

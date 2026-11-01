"""The MegaFon integration."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant

from .api import MegafonClient, async_create_session
from .const import (
    CONF_CHECK_INFO,
    CONF_COOKIE,
    CONF_ID_PARAM,
    CONF_SCAN_INTERVAL,
    CONF_VALIDATION_PARAM,
    DEFAULT_SCAN_INTERVAL_MINUTES,
)
from .coordinator import MegafonConfigEntry, MegafonCoordinator

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: MegafonConfigEntry) -> bool:
    session = async_create_session(hass)
    entry.async_on_unload(session.close)
    entry.async_on_unload(
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP,
            lambda event: hass.async_create_task(session.close()),
        )
    )
    client = MegafonClient(
        session,
        cookie=entry.data[CONF_COOKIE],
        check_info=entry.data.get(CONF_CHECK_INFO, ""),
        id_param=entry.data.get(CONF_ID_PARAM, ""),
        validation_param=entry.data.get(CONF_VALIDATION_PARAM, ""),
    )
    coordinator = MegafonCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: MegafonConfigEntry) -> None:
    coordinator = getattr(entry, "runtime_data", None)
    minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
    if coordinator is None or coordinator.update_interval != timedelta(
        minutes=minutes
    ):
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MegafonConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

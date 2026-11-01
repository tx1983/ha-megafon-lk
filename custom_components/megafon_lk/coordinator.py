"""Data update coordinator."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MegafonAuthError, MegafonClient, MegafonError
from .const import (
    CONF_COOKIE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class MegafonCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches balance, remainders and tariff in one go."""

    def __init__(
        self, hass: HomeAssistant, entry: MegafonConfigEntry, client: MegafonClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=entry.options.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
                )
            ),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            await self.client.async_ensure_session()
            balance = await self.client.async_get_balance()
            remainders = await self.client.async_get_remainders()
            tariff = await self.client.async_get_tariff()
        except MegafonAuthError as err:
            raise ConfigEntryAuthFailed(f"MegaFon session expired: {err}") from err
        except MegafonError as err:
            raise UpdateFailed(str(err)) from err

        self._persist_cookie()
        return {
            "balance": balance,
            "remainders": {
                item.get("remainderType"): item
                for item in remainders.get("remainders", [])
            },
            "tariff": tariff,
        }

    def _persist_cookie(self) -> None:
        """Write rotated cookies back to the entry so restarts keep working."""
        entry = self.config_entry
        if entry and self.client.cookie_header != entry.data[CONF_COOKIE]:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_COOKIE: self.client.cookie_header}
            )


type MegafonConfigEntry = ConfigEntry[MegafonCoordinator]

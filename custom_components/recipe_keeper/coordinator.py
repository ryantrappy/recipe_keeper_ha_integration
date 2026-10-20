"""Coordinate one persistent cloud client per account."""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ._vendor.recipe_keeper_client.async_client import AsyncRecipeClient
from ._vendor.recipe_keeper_client.cloud_protocol import (
    AuthenticationError,
    CloudHTTPError,
    ProbeError,
)
from .const import DOMAIN
from .shopping_import import ShoppingImporter

LOGGER = logging.getLogger(__name__)


class RecipeKeeperCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass, entry: ConfigEntry, client: AsyncRecipeClient) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(minutes=5),
        )
        self.client = client
        self.entry = entry
        self.importer = ShoppingImporter(hass, entry.entry_id)
        self.retry_at = 0.0

    @property
    def is_fresh(self) -> bool:
        if not self.last_update_success or not self.data:
            return False
        value = self.data.get("last_successful_sync_at")
        return bool(
            value
            and (datetime.now(UTC) - datetime.fromisoformat(value)).total_seconds()
            <= 600
        )

    async def _async_update_data(self) -> dict:
        if monotonic() < self.retry_at:
            raise UpdateFailed("Cloud retry delayed")
        try:
            return await self.client.sync()
        except AuthenticationError as exc:
            raise ConfigEntryAuthFailed("Recipe Keeper authentication failed") from exc
        except CloudHTTPError as exc:
            self.retry_at = monotonic() + exc.retry_after
            raise UpdateFailed("Recipe Keeper cloud request failed") from exc
        except (ProbeError, OSError, sqlite3.Error, ValueError) as exc:
            raise UpdateFailed("Recipe Keeper cloud or cache unavailable") from exc


type RecipeKeeperEntry = ConfigEntry[RecipeKeeperCoordinator]

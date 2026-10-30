"""Координатор данных интеграции ЕГДС.

Опрашивает сервер (GET /mobile1/barrier/addresses) и поддерживает сессию:
при 401/403 API-клиент сам обновляет access_token по refresh_token.
Если refresh не удался — ConfigEntryAuthFailed → HA запускает reauth-flow
(новый SMS-код).
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EgdsAuthError, EgdsClient, EgdsError
from .const import (
    COOLDOWN,
    DEFAULT_COOLDOWN,
    DEFAULT_OPEN_DURATION,
    DEFAULT_OPENING_TIME,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_VIDEO_FRAMERATE,
    DEFAULT_VIDEO_WIDTH,
    DOMAIN,
    OPEN_DURATION,
    OPENING_TIME,
    SCAN_INTERVAL,
    URL_API_BASE,
    VIDEO_FRAMERATE,
    VIDEO_WIDTH,
)

_LOGGER = logging.getLogger(__name__)


class EgdsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Общий координатор для всех сущностей аккаунта."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        session = async_get_clientsession(hass)
        self.client = EgdsClient(
            session,
            phone=entry.data[CONF_USERNAME],
            access_token=entry.data.get("access_token"),
            refresh_token=entry.data.get("refresh_token"),
            api_base=entry.data.get("api_base") or URL_API_BASE,
            video_framerate=entry.options.get(VIDEO_FRAMERATE, DEFAULT_VIDEO_FRAMERATE),
            video_viewsize=entry.options.get(VIDEO_WIDTH, DEFAULT_VIDEO_WIDTH) or None,
        )
        self.entry = entry
        self.last_open: dict[str, float] = {}

        interval = entry.options.get(SCAN_INTERVAL, DEFAULT_POLL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=interval),
        )

    # ------------------------------------------------------------------ #
    @property
    def opening_time(self) -> int:
        return int(self.entry.options.get(OPENING_TIME, DEFAULT_OPENING_TIME))

    @property
    def open_duration(self) -> int:
        return int(self.entry.options.get(OPEN_DURATION, DEFAULT_OPEN_DURATION))

    @property
    def cooldown(self) -> int:
        return int(self.entry.options.get(COOLDOWN, DEFAULT_COOLDOWN))

    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> dict[str, Any]:
        """Опрос сервера; при истёкшем refresh-токене — reauth."""
        try:
            barriers = await self.client.get_addresses()
        except EgdsAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EgdsError as err:
            raise UpdateFailed(str(err)) from err
        return {"barriers": barriers}

    # ------------------------------------------------------------------ #
    async def async_open_barrier(self, barrier: dict[str, Any]) -> str:
        """Открыть шлагбаум с защитой от повторных команд."""
        bar_id = str(barrier["barrier_id"])
        last = self.last_open.get(bar_id, 0.0)
        elapsed = time.time() - last
        if elapsed < self.cooldown:
            wait = int(self.cooldown - elapsed)
            raise EgdsError(f"Повторная команда возможна через {wait} с")
        await self.client.open_barrier(bar_id)
        self.last_open[bar_id] = time.time()
        # cover-сущности должны пересчитать предполагаемое состояние сразу,
        # даже если команда была отправлена кнопкой или сервисом
        self.async_update_listeners()
        return "открыто"

"""Координатор данных интеграции ЕГДС.

Опрашивает сервер (GET /mobile1/barrier/addresses) и бережно относится
к сессии:

  - access/refresh-токены хранятся в записи конфигурации и восстанавливаются
    после рестарта HA — повторный вход по SMS не требуется;
  - сервер ротонирует refresh_token при каждом обновлении: свежие токены
    сразу сохраняются обратно в запись (без перезагрузки интеграции);
  - access_token обновляется проактивно за 2 минуты до истечения
    (token_expires), а не по факту 401;
  - если refresh не удался — ConfigEntryAuthFailed → HA запускает
    reauth-flow (новый код доставки).
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
    CONF_TOKEN_EXPIRES,
    DEFAULT_COOLDOWN,
    DEFAULT_OPEN_DURATION,
    DEFAULT_OPENING_TIME,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_VIDEO_FRAMERATE,
    DEFAULT_VIDEO_SSL_VERIFY,
    DEFAULT_VIDEO_WIDTH,
    DOMAIN,
    OPEN_DURATION,
    OPENING_TIME,
    PROACTIVE_REFRESH_MARGIN,
    SCAN_INTERVAL,
    URL_API_BASE,
    VIDEO_FRAMERATE,
    VIDEO_SSL_VERIFY,
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
            video_ssl_verify=entry.options.get(
                VIDEO_SSL_VERIFY, DEFAULT_VIDEO_SSL_VERIFY
            ),
        )
        # Восстановить срок действия токена, если он был сохранён
        saved_expires = entry.data.get(CONF_TOKEN_EXPIRES)
        if saved_expires:
            try:
                self.client.token_expires = float(saved_expires)
            except (TypeError, ValueError):
                pass
        # Свежие токены сразу сохраняем в запись (сервер ротонирует
        # refresh_token — потеря его означает повторный вход по SMS)
        self.client.on_tokens_update = self._persist_tokens
        # Флаг «это обновление записи сделал я сам (токены) — не перезагружать»
        self.suppress_reload = False
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
    def _persist_tokens(self, tokens: dict[str, Any]) -> None:
        """Сохранить обновлённые токены в запись конфигурации.

        Вызывается из колбэка клиента при каждом refresh/login. Запись
        меняется без перезагрузки интеграции (suppress_reload) — клиент
        уже работает с новыми токенами, а сохранение нужно только для
        следующего старта HA.
        """
        entry = self.entry
        new_data = {**entry.data, "access_token": tokens["access_token"]}
        # refresh_token обновляем только если сервер его прислал
        if tokens.get("refresh_token"):
            new_data["refresh_token"] = tokens["refresh_token"]
        if tokens.get("token_expires") is not None:
            new_data[CONF_TOKEN_EXPIRES] = tokens["token_expires"]
        if new_data == entry.data:
            return
        self.suppress_reload = True
        self.hass.config_entries.async_update_entry(entry, data=new_data)
        _LOGGER.debug("Обновлённые токены сохранены в запись конфигурации")

    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> dict[str, Any]:
        """Опрос сервера; при истёкшем refresh-токене — reauth."""
        try:
            # Проактивный refresh: за 2 минуты до истечения access_token,
            # чтобы не ловить 401 и не терять запросы опроса
            expires = self.client.token_expires
            if (
                self.client.refresh_token
                and expires is not None
                and expires - time.time() < PROACTIVE_REFRESH_MARGIN
            ):
                await self.client.async_refresh_token()
            barriers = await self.client.get_addresses()
        except EgdsAuthError as err:
            raise ConfigEntryAuthFailed(
                f"Сессия ЕГДС истекла: {err}. Откройте «Настройки → ЕГДС → "
                "Повторная авторизация» и введите новый код доставки."
            ) from err
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

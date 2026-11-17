"""Интеграция ЕГДС для Home Assistant."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_extract_entity_ids

from .brand import BrandIconView
from .const import CONF_RESTREAM_ENABLED, DOMAIN, PLATFORMS
from .coordinator import EgdsCoordinator
from .entity_registry import OPEN_ENTITIES
from .restream import RestreamView, ensure_restream_token, rotate_restream_token

_LOGGER = logging.getLogger(__name__)

OPEN_BARRIER_SCHEMA = vol.Schema(
    {vol.Required(ATTR_ENTITY_ID): cv.entity_ids}
)


async def _async_handle_open_barrier(call: ServiceCall) -> None:
    """Сервис egds.open_barrier — открыть выбранные шлагбаумы."""
    entity_ids = await async_extract_entity_ids(call.hass, call)
    errors: list[str] = []
    for entity_id in entity_ids:
        entity = OPEN_ENTITIES.get(entity_id)
        if entity is None:
            continue
        try:
            await entity.async_open_barrier()
        except Exception as err:  # noqa: BLE001
            errors.append(f"{entity_id}: {err}")
            _LOGGER.error("Не удалось открыть %s: %s", entity_id, err)
    if errors:
        raise HomeAssistantError("; ".join(errors))


async def _async_handle_regenerate_restream_token(call: ServiceCall) -> None:
    """Сервис egds.regenerate_restream_token — перевыпустить токен рестрима.

    Токен хранится в entry.data и никогда не попадает в журнал,
    атрибуты сущностей или диагностику. После ротации интеграция
    перезагружается (update listener) — прежние ссылки Frigate
    перестают работать сразу.
    """
    rotated = 0
    for entry in call.hass.config_entries.async_entries(DOMAIN):
        if not entry.options.get(CONF_RESTREAM_ENABLED, False):
            continue
        if rotate_restream_token(call.hass, entry):
            rotated += 1
    if not rotated:
        raise HomeAssistantError(
            "Нет записей с включённым «Локальный рестрим» — токен не перевыпущен"
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Настройка интеграции из UI."""
    # Бренд-иконка и локальный рестрим для NVR — общие для всех записей
    if not hass.data.get(f"{DOMAIN}_views_registered"):
        hass.http.register_view(BrandIconView(hass))
        hass.http.register_view(RestreamView())
        hass.data[f"{DOMAIN}_views_registered"] = True

    # Токен рестрима (создаётся при первом включении опции)
    ensure_restream_token(hass, entry)

    coordinator = EgdsCoordinator(hass, entry)

    # Первое подключение: токены уже получены в config_flow
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    # Persist токенов мог произойти при первом refresh (до регистрации
    # слушателя) — сбрасываем флаг, чтобы последующие изменения опций
    # гарантированно перезагружали интеграцию
    coordinator.suppress_reload = 0

    if not hass.services.has_service(DOMAIN, "open_barrier"):
        hass.services.async_register(
            DOMAIN, "open_barrier", _async_handle_open_barrier, OPEN_BARRIER_SCHEMA
        )
        hass.services.async_register(
            DOMAIN, "regenerate_restream_token", _async_handle_regenerate_restream_token
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Выгрузка интеграции."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Перезагрузка при изменении записи.

    Координатор сам сохраняет обновлённые refresh-токены в запись
    (suppress_reload) — в этом случае перезагрузка не нужна: клиент уже
    работает с новыми токенами. Перезагружаем только при реальном
    изменении (опции, повторная авторизация).
    """
    coordinator = getattr(entry, "runtime_data", None)
    # suppress_reload — счётчик: одно сохранение токенов гасит ровно один
    # вызов listener-а (см. EgdsCoordinator._persist_tokens)
    if coordinator is not None and getattr(coordinator, "suppress_reload", 0) > 0:
        coordinator.suppress_reload -= 1
        return
    await hass.config_entries.async_reload(entry.entry_id)

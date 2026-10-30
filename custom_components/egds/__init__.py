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

from .const import DOMAIN, PLATFORMS
from .coordinator import EgdsCoordinator
from .entity_registry import OPEN_ENTITIES

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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Настройка интеграции из UI."""
    coordinator = EgdsCoordinator(hass, entry)

    # Первое подключение: токены уже получены в config_flow
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    if not hass.services.has_service(DOMAIN, "open_barrier"):
        hass.services.async_register(
            DOMAIN, "open_barrier", _async_handle_open_barrier, OPEN_BARRIER_SCHEMA
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Выгрузка интеграции."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Перезагрузка при изменении опций."""
    await hass.config_entries.async_reload(entry.entry_id)

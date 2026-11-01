"""Кнопки открытия шлагбаумов ЕГДС."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .brand import brand_icon_url
from .const import DOMAIN, MANUFACTURER, MODEL_BARRIER
from .coordinator import EgdsCoordinator
from .entity_registry import register_open_entity, unregister_open_entity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создать по кнопке «Открыть» на каждый доступный КПУ."""
    coordinator: EgdsCoordinator = entry.runtime_data
    entities: list[EgdsOpenButton] = []
    for barrier in coordinator.data.get("barriers", []):
        entities.append(EgdsOpenButton(coordinator, entry.entry_id, barrier))
    async_add_entities(entities)


class EgdsOpenButton(CoordinatorEntity, ButtonEntity):
    """Кнопка отправки команды открытия КПУ."""

    _attr_has_entity_name = True
    _attr_name = "Открыть"
    _attr_icon = "mdi:boom-gate-arrow-up-outline"
    _attr_entity_picture = brand_icon_url()

    def __init__(
        self,
        coordinator: EgdsCoordinator,
        entry_id: str,
        barrier: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._barrier = barrier
        bar_id = str(barrier["barrier_id"])
        self._bar_id = bar_id
        self._attr_unique_id = f"{entry_id}_open_{bar_id}"
        name_parts = [
            barrier.get("address_name") or f"КПУ {bar_id}"
        ]
        if barrier.get("group_name"):
            name_parts.append(str(barrier["group_name"]))
        name_parts.append(str(barrier.get("barrier_name") or bar_id))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"barrier_{bar_id}")},
            name=" · ".join(name_parts),
            manufacturer=MANUFACTURER,
            model=MODEL_BARRIER,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "barrier_id": self._barrier.get("barrier_id"),
            "barrier_number": self._barrier.get("barrier_number"),
            "address_id": self._barrier.get("address_id"),
            "address_name": self._barrier.get("address_name"),
            "group_name": self._barrier.get("group_name"),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        register_open_entity(self.entity_id, self)

    async def async_will_remove_from_hass(self) -> None:
        unregister_open_entity(self.entity_id)
        await super().async_will_remove_from_hass()

    async def async_press(self) -> None:
        """Отправить команду открытия."""
        await self.async_open_barrier()

    async def async_open_barrier(self) -> None:
        """Метод для сервиса egds.open_barrier."""
        try:
            await self.coordinator.async_open_barrier(self._barrier)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(str(err)) from err
        _LOGGER.info(
            "%s: команда открытия отправлена",
            self._barrier.get("address_name"),
        )

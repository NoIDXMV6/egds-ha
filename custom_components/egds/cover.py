"""Шлагбаумы ЕГДС как cover-сущности.

Сервер не сообщает состояние шлагбаума, поэтому состояние «предполагаемое»
(как и в приложении): после команды — «открывается» opening_time секунд,
затем «открыто» open_duration секунд, потом «закрыто».
Поддерживается только действие open (закрытие не предусмотрено протоколом).
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MANUFACTURER, MODEL_BARRIER
from .coordinator import EgdsCoordinator
from .entity_registry import register_open_entity, unregister_open_entity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создать cover-сущности для каждого доступного КПУ."""
    coordinator: EgdsCoordinator = entry.runtime_data
    entities: list[EgdsBarrierCover] = []
    for barrier in coordinator.data.get("barriers", []):
        entities.append(EgdsBarrierCover(coordinator, entry.entry_id, barrier))
    async_add_entities(entities)


class EgdsBarrierCover(CoordinatorEntity, CoverEntity):
    """Шлагбаум (КПУ) с предполагаемым состоянием."""

    _attr_has_entity_name = True
    _attr_name = "Шлагбаум"
    _attr_supported_features = CoverEntityFeature.OPEN
    _attr_assumed_state = True
    _attr_icon = "mdi:boom-gate"

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
        self._attr_unique_id = f"{entry_id}_cover_{bar_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"barrier_{bar_id}")},
            name=self._device_name(),
            manufacturer=MANUFACTURER,
            model=MODEL_BARRIER,
        )
        self._unsub_timers: list[Any] = []

    def _device_name(self) -> str:
        parts = [self._barrier.get("address_name") or "КПУ"]
        if self._barrier.get("group_name"):
            parts.append(str(self._barrier["group_name"]))
        parts.append(str(self._barrier.get("barrier_name") or self._bar_id))
        return " · ".join(parts)

    # ------------------------------------------------------------------ #
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.coordinator.last_open.get(self._bar_id)
        return {
            "barrier_id": self._barrier.get("barrier_id"),
            "barrier_number": self._barrier.get("barrier_number"),
            "address_id": self._barrier.get("address_id"),
            "address_name": self._barrier.get("address_name"),
            "group_name": self._barrier.get("group_name"),
            "last_open_command": (
                dt_util.as_local(dt_util.utc_from_timestamp(last)).isoformat()
                if last
                else None
            ),
            "state_is_assumed": True,
        }

    def _seconds_since_open(self) -> float | None:
        last = self.coordinator.last_open.get(self._bar_id)
        if last is None:
            return None
        return time.time() - last

    @property
    def is_opening(self) -> bool | None:
        elapsed = self._seconds_since_open()
        if elapsed is None:
            return False
        return elapsed < self.coordinator.opening_time

    @property
    def is_closed(self) -> bool | None:
        elapsed = self._seconds_since_open()
        if elapsed is None:
            return True
        return elapsed >= self.coordinator.opening_time + self.coordinator.open_duration

    # ------------------------------------------------------------------ #
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        register_open_entity(self.entity_id, self)

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Отправить команду открытия."""
        try:
            await self.coordinator.async_open_barrier(self._barrier)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(str(err)) from err
        _LOGGER.info("%s: команда открытия отправлена", self._device_name())

    async def async_open_barrier(self) -> None:
        """Метод для сервиса egds.open_barrier."""
        await self.async_open_cover()

    # ------------------------------------------------------------------ #
    @callback
    def _handle_coordinator_update(self) -> None:
        """Обновление от координатора (в т.ч. после команды открытия)."""
        self._arm_transition_timers()
        self.async_write_ha_state()

    @callback
    def _arm_transition_timers(self) -> None:
        """Запланировать перерисовку в моменты переходов opening→open→closed."""
        for unsub in self._unsub_timers:
            unsub()
        self._unsub_timers = []

        last = self.coordinator.last_open.get(self._bar_id)
        if last is None:
            return

        def _refresh(event: Event | None = None) -> None:
            self.async_write_ha_state()

        opening_end = dt_util.utc_from_timestamp(
            last + self.coordinator.opening_time
        )
        open_end = dt_util.utc_from_timestamp(
            last + self.coordinator.opening_time + self.coordinator.open_duration
        )
        now = dt_util.utcnow()
        if opening_end > now:
            self._unsub_timers.append(
                async_track_point_in_utc_time(self.hass, _refresh, opening_end)
            )
        if open_end > now:
            self._unsub_timers.append(
                async_track_point_in_utc_time(self.hass, _refresh, open_end)
            )

    async def async_will_remove_from_hass(self) -> None:
        unregister_open_entity(self.entity_id)
        for unsub in self._unsub_timers:
            unsub()
        self._unsub_timers = []
        await super().async_will_remove_from_hass()

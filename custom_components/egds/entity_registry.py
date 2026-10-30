"""Реестр сущностей, поддерживающих сервис открытия шлагбаума."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # только для подсказок типов
    from .button import EgdsOpenButton
    from .cover import EgdsBarrierCover


class OpenableEntity(Protocol):
    """Сущность, умеющая открывать шлагбаум."""

    async def async_open_barrier(self) -> None: ...


#: entity_id -> сущность (заполняется в async_added_to_hass)
OPEN_ENTITIES: dict[str, OpenableEntity] = {}


def register_open_entity(entity_id: str, entity: OpenableEntity) -> None:
    OPEN_ENTITIES[entity_id] = entity


def unregister_open_entity(entity_id: str) -> None:
    OPEN_ENTITIES.pop(entity_id, None)

"""Камеры КПУ ЕГДС: MJPEG через HTTP-прокси с Basic-авторизацией.

Видео сервиса ЕГДС — это HTTP-эндпоинт камеры:
  https://{host}:{port}/rsapi/snapshot?id={Guid}  — JPEG-кадр;
  https://{host}:{port}/rsapi/mjpeg?id={Guid}&framerate=..&viewSize=..
  — MJPEG-поток (сырые JPEG-кадры подряд).

Авторизация: Basic {Login}:{Password} — параметры приходят в ответе
/mobile1/barrier/addresses для каждой камеры.

Реализация:
- camera_image() — одиночный JPEG-кадр (snapshot);
- handle_async_mjpeg_stream() — живой поток: кадры из MJPEG-потока
  заворачиваются в multipart/x-mixed-replace и отдаются в браузер/Lovelace.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import EgdsError, JPEG_END, JPEG_START
from .const import DOMAIN, MANUFACTURER, MODEL_CAMERA
from .coordinator import EgdsCoordinator
from .diag import warn_limited

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создать camera-сущности для каждой камеры КПУ."""
    coordinator: EgdsCoordinator = entry.runtime_data
    entities: list[EgdsBarrierCamera] = []
    for barrier in coordinator.data.get("barriers", []):
        for cam in barrier.get("cameras", []):
            entities.append(
                EgdsBarrierCamera(
                    coordinator,
                    entry.entry_id,
                    barrier,
                    cam,
                )
            )
    async_add_entities(entities)


class EgdsBarrierCamera(CoordinatorEntity, Camera):
    """Живая камера КПУ (MJPEG / snapshot)."""

    _attr_has_entity_name = True
    _attr_name = "Камера"
    _attr_supported_features = CameraEntityFeature(0)

    def __init__(
        self,
        coordinator: EgdsCoordinator,
        entry_id: str,
        barrier: dict[str, Any],
        cam: dict[str, Any],
    ) -> None:
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self._barrier = barrier
        self._cam = cam
        guid = str(cam.get("guid") or cam.get("id"))
        bar_id = str(barrier["barrier_id"])
        self._attr_unique_id = f"{entry_id}_cam_{bar_id}_{guid}"
        name_parts = [barrier.get("address_name") or f"КПУ {bar_id}"]
        if barrier.get("group_name"):
            name_parts.append(str(barrier["group_name"]))
        name_parts.append(str(barrier.get("barrier_name") or bar_id))
        cam_name = str(cam.get("name") or "Камера")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"barrier_{bar_id}")},
            name=" · ".join(name_parts),
            manufacturer=MANUFACTURER,
            model=MODEL_CAMERA,
        )
        self._attr_translation_placeholders = {"camera_name": cam_name}
        self._frame: bytes | None = None
        self._frame_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        """Имя сущности (камера конкретного КПУ)."""
        base = self._barrier.get("address_name") or "КПУ"
        return f"{base} · {self._cam.get('name') or 'Камера'}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {
            "camera_guid": self._cam.get("guid"),
            "camera_host": self._cam.get("host"),
            "camera_port": self._cam.get("port"),
            "camera_url": self.coordinator.client.snapshot_url(self._cam),
            "barrier_id": self._barrier.get("barrier_id"),
            "address_name": self._barrier.get("address_name"),
        }
        # URL без учётных данных — атрибуты видны в интерфейсе
        return attrs

    # ------------------------------------------------------------------ #
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Одиночный JPEG-кадр (rsapi/snapshot)."""
        async with self._frame_lock:
            frame = await self.coordinator.client.fetch_snapshot(self._cam)
            if frame:
                self._frame = frame
            return self._frame

    async def handle_async_mjpeg_stream(self, request):
        """Живой MJPEG-поток: кадры из rsapi/mjpeg → multipart."""
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frameboundary"
            },
        )
        await response.prepare(request)
        try:
            async for frame in self.coordinator.client.iter_mjpeg_frames(self._cam):
                self._frame = frame
                await response.write(
                    b"--frameboundary\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
        except (EgdsError, ConnectionResetError, asyncio.CancelledError) as err:
            if isinstance(err, asyncio.CancelledError):
                raise
            warn_limited(
                _LOGGER,
                f"mjpeg_{self.entity_id}",
                "MJPEG-поток %s прерван: %s: %s "
                "(подробности в журнале интеграции egds)",
                self.entity_id,
                type(err).__name__,
                err,
            )
        finally:
            await response.write_eof()
        return response

    async def async_will_remove_from_hass(self) -> None:
        await Camera.async_will_remove_from_hass(self)
        await CoordinatorEntity.async_will_remove_from_hass(self)

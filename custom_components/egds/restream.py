"""Локальный рестрим камер ЕГДС для NVR (Frigate, go2rtc, iSpy…).

Зачем: видео ЕГДС живёт на камерах за сервером авторизации — поток
/rsapi/mjpeg требует Basic-авторизации, а логин/пароль камеры сервер
регулярно ротирует. Интеграция сама держит актуальные учётные данные
(перечитывает их при ротации) и раздаёт MJPEG-кадры по локальному
HTTP — этого входа достаточно для детекции машин/людей во Frigate:

    ffmpeg:
      inputs:
        - path: http://homeassistant:8123/api/egds/restream/…/….mjpg?token=…
          input_args: preset-http-mjpeg
          roles: [detect]

Доступ: адрес без авторизации HA, но защищён случайным токеном
(CONF_RESTREAM_TOKEN), который генерируется один раз при включении
«Локальный рестрим» в опциях интеграции и показывается в атрибуте
frigate_input камеры. Рестрим по умолчанию выключен.
"""
from __future__ import annotations

import secrets

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_RESTREAM_ENABLED, CONF_RESTREAM_TOKEN, DOMAIN

_FRIGATE_HOST = "homeassistant:8123"  # DNS-псевдоним HA core внутри HAOS-аддонов


def ensure_restream_token(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Токен рестрима: создаётся один раз и хранится в entry.data.

    Возвращает токен, если рестрим включён в опциях, иначе None.
    """
    if not entry.options.get(CONF_RESTREAM_ENABLED, False):
        return None
    token = entry.data.get(CONF_RESTREAM_TOKEN)
    if not token:
        token = secrets.token_urlsafe(24)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_RESTREAM_TOKEN: token}
        )
    return token


def frigate_input_url(entry: ConfigEntry, key: str) -> str | None:
    """Готовый вход для config.yml Frigate (или None, если рестрим выключен)."""
    token = entry.data.get(CONF_RESTREAM_TOKEN)
    if not token:
        return None
    return (
        f"http://{_FRIGATE_HOST}/api/{DOMAIN}/restream/"
        f"{entry.entry_id}/{key}.mjpg?token={token}"
    )


def register_restream(hass: HomeAssistant, entry_id: str, key: str, entity) -> None:
    """Зарегистрировать камеру для раздачи по рестриму."""
    registry = hass.data.setdefault(DOMAIN, {}).setdefault("restream", {})
    registry[f"{entry_id}/{key}"] = entity


def unregister_restream(hass: HomeAssistant, entry_id: str) -> None:
    """Снять с раздачи все камеры записи конфигурации."""
    registry = hass.data.get(DOMAIN, {}).get("restream", {})
    for key in [k for k in registry if k.startswith(f"{entry_id}/")]:
        registry.pop(key, None)


def unregister_restream_entity(hass: HomeAssistant, entry_id: str, key: str) -> None:
    """Снять с раздачи одну камеру (при удалении сущности)."""
    hass.data.get(DOMAIN, {}).get("restream", {}).pop(f"{entry_id}/{key}", None)


class RestreamView(HomeAssistantView):
    """MJPEG-поток камеры по токену (для Frigate и других NVR)."""

    url = f"/api/{DOMAIN}/restream/{{entry_id}}/{{key}}.mjpg"
    extra_urls = [f"/api/{DOMAIN}/restream/{{entry_id}}/{{key}}"]
    name = f"api:{DOMAIN}:restream"
    requires_auth = False

    async def get(self, request: web.Request, entry_id: str, key: str):
        hass: HomeAssistant = request.app["hass"]
        entry = hass.config_entries.async_get_entry(entry_id)
        expected = (entry.data.get(CONF_RESTREAM_TOKEN) if entry else None) or ""
        token = request.query.get("token", "")
        if not expected or not token or not secrets.compare_digest(token, expected):
            return web.Response(status=401, text="invalid token")
        entity = hass.data.get(DOMAIN, {}).get("restream", {}).get(f"{entry_id}/{key}")
        if entity is None:
            return web.Response(status=404, text="camera not registered")
        return await entity.handle_async_mjpeg_stream(request)

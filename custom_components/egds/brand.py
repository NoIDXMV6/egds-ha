"""Бренд-иконка интеграции: оригинальная иконка приложения ЕГДС
с логотипом Home Assistant в правом нижнем углу.

Отдаётся как статический PNG без авторизации HA — ссылка используется
в entity_picture сущностей (значок в more-info, на Tile-карточках) и на
Lovelace-карточках. Ссылка ведёт только на картинку и секретов не содержит.

Начиная с HA 2026.3 тот же файл brand/icon.png автоматически показывается
в Настройки → Устройства и сервисы (механизм локальных бренд-файлов).
"""
from __future__ import annotations

from importlib import resources

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN

_BRAND_URL = f"/api/{DOMAIN}/brand/icon.png"


def brand_icon_url() -> str:
    """URL бренд-иконки для entity_picture."""
    return _BRAND_URL


class BrandIconView(HomeAssistantView):
    """PNG с иконкой интеграции (без авторизации, только картинка)."""

    url = _BRAND_URL
    name = f"api:{DOMAIN}:brand_icon"
    requires_auth = False

    def __init__(self) -> None:
        self._body: bytes | None = None

    def _load(self) -> bytes:
        if self._body is None:
            self._body = (
                resources.files(__package__) / "brand" / "icon.png"
            ).read_bytes()
        return self._body

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(
            body=self._load(),
            content_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

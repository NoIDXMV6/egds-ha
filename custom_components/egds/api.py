"""API-клиент сервиса ЕГДС (client.egdsk.ru).

Протокол восстановлен обратной разработкой приложения ЕГДС 3.3.25
(.NET MAUI, пакет ru.egds.EgdsMobile):

  - аутентификация: OAuth2 на /oauth/token, form-urlencoded,
    заголовок Authorization: Basic base64("AndroidApp_1.3:Secret#1");
  - grant_type=sms&phone_number={телефон}         — выслать код по SMS;
  - grant_type=password&username={телефон}&password={код} — получить токены
    (access_token, refresh_token, token_expires, base_token);
  - grant_type=refresh_token&refresh_token={rt}   — обновить токены;
  - GET  /mobile1/barrier/addresses               — адреса с шлагбаумами
    (Bearer-токен): Address{Id, Name, Barriers:[EBarrier{Id, Number,
    GroupName, Name, Cameras:[Camera{Id, Name, Host, Port, Guid, Login,
    Password}]}]};
  - /mobile1/barrier/open/{id}                    — открыть шлагбаум
    (Bearer-токен);
  - видео: https://{host}:{port}/rsapi/snapshot?id={Guid} — JPEG-кадр,
    https://{host}:{port}/rsapi/mjpeg?id={Guid}&framerate=..&viewSize=..
    — MJPEG-поток; авторизация Basic {Login}:{Password} камеры.

Модуль не зависит от Home Assistant и может тестироваться отдельно.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Any, Callable

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import (
    CLIENT_ID,
    CLIENT_SECRET,
    DEFAULT_TIMEOUT,
    GRANT_PASSWORD,
    GRANT_REFRESH,
    GRANT_SMS,
    URL_API_BASE,
    URL_OAUTH,
)
from .diag import warn_limited

_LOGGER = logging.getLogger(__name__)

# JPEG-маркеры для разбора «сырого» MJPEG-потока (как в приложении:
# VideoService.ScanJpegStart / ScanJpegStop)
JPEG_START = b"\xff\xd8\xff"
JPEG_END = b"\xff\xd9"


class EgdsError(Exception):
    """Базовая ошибка API ЕГДС."""


class EgdsAuthError(EgdsError):
    """Ошибка аутентификации (неверный код, истёкший токен)."""


class EgdsDeliveryError(EgdsAuthError):
    """Сервер не может доставить код: не подключён ни один канал.

    ЕГДС доставляет код входа через: SMS/звонок, бота MAX, Telegram
    или подтверждённый email. Если для аккаунта не подключено ничего,
    сервер отвечает error=verification_method_unavailable.
    Каналы подключаются в приложении (Настройки уведомлений)
    или в мини-приложении lk.egdsk.ru (мессенджер MAX).
    """


class EgdsConnectionError(EgdsError):
    """Ошибка соединения с сервером."""


def normalize_phone(phone: str) -> str:
    """Привести телефон к виду 7XXXXXXXXXX (как в приложении)."""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) == 10 and digits[0] != "7":
        digits = "7" + digits
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    return digits


def _case_insensitive(data: Any, *names: str) -> Any:
    """Найти значение в словаре без учёта регистра ключей."""
    if not isinstance(data, dict):
        return None
    lowered = {str(k).lower(): v for k, v in data.items()}
    for name in names:
        if name in lowered:
            return lowered[name]
    return None


def _as_list(data: Any) -> list[Any]:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    return [data]


def parse_cameras(item: Any) -> list[dict[str, Any]]:
    """Нормализовать список камер шлагбаума."""
    cams = _as_list(_case_insensitive(item or {}, "cameras", "cams", "cam"))
    out: list[dict[str, Any]] = []
    for cam in cams:
        if not isinstance(cam, dict):
            continue
        entry = {
            "id": _case_insensitive(cam, "id"),
            "name": _case_insensitive(cam, "name") or "Камера",
            "host": _case_insensitive(cam, "host"),
            "port": _case_insensitive(cam, "port"),
            "guid": _case_insensitive(cam, "guid", "guid_id", "camera_guid"),
            "login": _case_insensitive(cam, "login", "user", "username"),
            "password": _case_insensitive(cam, "password", "pass"),
        }
        if entry["guid"] is None and entry["id"] is not None:
            entry["guid"] = entry["id"]
        if entry["host"] and entry["guid"] is not None:
            out.append(entry)
    return out


def parse_addresses(payload: Any) -> list[dict[str, Any]]:
    """Разобрать ответ /mobile1/barrier/addresses в плоский список КПУ.

    Принимает любую из форм: [Address], {data: [...]}, {addresses: [...]}.
    Каждый КПУ: {address_id, address_name, barrier_id, barrier_number,
    group_name, barrier_name, cameras: [...]}.
    """
    data = payload
    if isinstance(data, dict):
        inner = _case_insensitive(data, "data", "addresses", "items", "result")
        data = inner if inner is not None else data
    barriers: list[dict[str, Any]] = []
    for addr in _as_list(data):
        if not isinstance(addr, dict):
            continue
        addr_id = _case_insensitive(addr, "id", "addressid", "address_id")
        addr_name = _case_insensitive(addr, "name", "addressname", "address_name", "address")
        for bar in _as_list(_case_insensitive(addr, "barriers", "barrierlist", "barrierslist")):
            if not isinstance(bar, dict):
                continue
            bar_id = _case_insensitive(bar, "id", "barrierid", "barrier_id")
            if bar_id is None:
                continue
            barriers.append(
                {
                    "address_id": addr_id,
                    "address_name": addr_name or f"Адрес {addr_id}",
                    "barrier_id": str(bar_id),
                    "barrier_number": _case_insensitive(bar, "number", "barriernumber"),
                    "group_name": _case_insensitive(bar, "groupname", "group_name", "group"),
                    "barrier_name": _case_insensitive(bar, "name", "barriername", "barrier_name")
                    or f"Шлагбаум {bar_id}",
                    "cameras": parse_cameras(bar),
                }
            )
    return barriers


class EgdsClient:
    """Асинхронный клиент ЕГДС (OAuth2 + /mobile1)."""

    def __init__(
        self,
        session: ClientSession,
        phone: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
        api_base: str = URL_API_BASE,
        video_framerate: int | None = 2,
        video_viewsize: str | None = None,
        video_scheme: str = "https",
        video_ssl_verify: bool = False,
    ) -> None:
        self._session = session
        self._phone = normalize_phone(phone)
        self._api_base = api_base.rstrip("/")
        self._framerate = video_framerate
        self._viewsize = video_viewsize
        self._video_scheme = video_scheme.rstrip(":/")
        # Сертификаты камер почти всегда самоподписанные (хост — IP),
        # поэтому по умолчанию их проверка выключена — как в приложении.
        # Влияет только на запросы к камерам, API-сервер проверяется всегда.
        self._video_ssl_verify = video_ssl_verify

        self.access_token: str | None = access_token
        self.refresh_token: str | None = refresh_token
        self.token_expires: float | None = None
        self.base_token: str | None = None

        # Колбэк сохранения токенов (назначается координатором): сервер ЕГДС
        # ротонирует refresh_token при каждом обновлении — свежий токен надо
        # сразу сохранять в запись конфигурации, иначе после рестарта HA
        # refresh-цепочка рвётся и требуется повторный вход по SMS.
        self.on_tokens_update: Callable[[dict[str, Any]], None] | None = None

        basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        self._oauth_headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    # ------------------------------------------------------------------ #
    # Низкоуровневые запросы
    # ------------------------------------------------------------------ #
    async def _oauth_request(self, body: str) -> dict[str, Any]:
        """POST /oauth/token с form-телом и Basic-авторизацией клиента."""
        url = f"{self._api_base}{URL_OAUTH}"
        try:
            async with self._session.post(
                url,
                data=body,
                headers=self._oauth_headers,
                timeout=ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as resp:
                text = await resp.text()
                try:
                    payload: dict[str, Any] = await resp.json(content_type=None)
                except ValueError:
                    payload = {}
                if resp.status == 200:
                    return payload
                error = str(
                    payload.get("error") or _extract_error(text) or f"HTTP {resp.status}"
                )
                description = str(payload.get("error_description") or "")
                _LOGGER.debug("oauth/token: %s %s", error, description)
                if error == "invalid_grant":
                    raise EgdsAuthError(description or "Отказ авторизации")
                if error == "verification_method_unavailable":
                    raise EgdsDeliveryError(description or "Нет доступных способов доставки кода")
                raise EgdsError(description or error)
        except EgdsError:
            raise
        except (ClientError, asyncio.TimeoutError, OSError) as err:
            raise EgdsConnectionError(f"Нет связи с сервером: {err}") from err

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        retry_auth: bool = True,
    ) -> Any:
        """Запрос к /mobile1 с Bearer-токеном; авто-refresh при 401."""
        if not self.access_token:
            raise EgdsAuthError("Нет токена доступа")
        url = f"{self._api_base}{path}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            async with self._session.request(
                method,
                url,
                data=body,
                headers=headers,
                timeout=ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as resp:
                if resp.status in (401, 403) and retry_auth:
                    _LOGGER.info("Токен отклонён (%s) — обновляю", resp.status)
                    await self.async_refresh_token()
                    return await self._api_request(
                        method, path, body=body, retry_auth=False
                    )
                text = await resp.text()
                if resp.status >= 400:
                    raise EgdsError(f"HTTP {resp.status} от {path}: {text[:200]}")
                if not text:
                    return None
                try:
                    return await resp.json(content_type=None)
                except ValueError:
                    return text
        except EgdsError:
            raise
        except (ClientError, asyncio.TimeoutError, OSError) as err:
            raise EgdsConnectionError(f"Нет связи с сервером: {err}") from err

    # ------------------------------------------------------------------ #
    # Аутентификация
    # ------------------------------------------------------------------ #
    async def request_sms_code(self) -> None:
        """Выслать SMS-код на телефон (grant_type=sms)."""
        body = GRANT_SMS.format(phone=self._phone)
        await self._oauth_request(body)
        _LOGGER.info("SMS-код запрошен для %s**", self._phone[:4])

    async def login_with_code(self, code: str) -> None:
        """Обменять SMS-код на токены (grant_type=password)."""
        code_digits = "".join(ch for ch in str(code) if ch.isdigit())
        body = GRANT_PASSWORD.format(phone=self._phone, code=code_digits)
        payload = await self._oauth_request(body)
        self._apply_tokens(payload)
        _LOGGER.info("Вход выполнен, токен получен")

    async def async_refresh_token(self) -> None:
        """Обновить access_token по refresh_token."""
        if not self.refresh_token:
            raise EgdsAuthError("Нет refresh_token — требуется повторный вход")
        body = GRANT_REFRESH.format(token=self.refresh_token)
        try:
            payload = await self._oauth_request(body)
        except EgdsAuthError as err:
            self.refresh_token = None
            raise EgdsAuthError(f"Сессия истекла: {err}") from err
        self._apply_tokens(payload)

    def _apply_tokens(self, payload: dict[str, Any]) -> None:
        token = _case_insensitive(payload, "access_token", "accessstoken")
        if not token:
            raise EgdsAuthError("Сервер не вернул access_token")
        self.access_token = str(token)
        refresh = _case_insensitive(payload, "refresh_token", "refreshtoken")
        if refresh:
            self.refresh_token = str(refresh)
        base = _case_insensitive(payload, "base_token", "basetoken")
        self.base_token = str(base) if base else None
        expires = _case_insensitive(payload, "token_expires", "expires_in", "expires")
        if expires is not None:
            try:
                self.token_expires = time.time() + float(expires)
            except (TypeError, ValueError):
                self.token_expires = None
        # Сообщаем наверх (координатор сохранит токены в запись конфигурации)
        if self.on_tokens_update is not None:
            try:
                self.on_tokens_update(
                    {
                        "access_token": self.access_token,
                        "refresh_token": self.refresh_token,
                        "token_expires": self.token_expires,
                    }
                )
            except Exception:  # noqa: BLE001 — сохранение не должно ломать API
                _LOGGER.exception("Не удалось сохранить обновлённые токены")

    # ------------------------------------------------------------------ #
    # Данные и команды
    # ------------------------------------------------------------------ #
    async def get_addresses(self) -> list[dict[str, Any]]:
        """GET /mobile1/barrier/addresses — плоский список КПУ с камерами."""
        payload = await self._api_request("GET", "/mobile1/barrier/addresses")
        return parse_addresses(payload)

    async def open_barrier(self, barrier_id: str | int) -> None:
        """Открыть шлагбаум: POST /mobile1/barrier/open/{id}.

        Если сервер не принимает такой формат — пробуем варианты
        ?id= и GET (протокол приложения допускает разные сборки).
        """
        bar_id = str(barrier_id)
        attempts = (
            ("POST", f"/mobile1/barrier/open/{bar_id}"),
            ("POST", f"/mobile1/barrier/open?id={bar_id}"),
            ("GET", f"/mobile1/barrier/open/{bar_id}"),
        )
        last_err: EgdsError | None = None
        for method, path in attempts:
            try:
                await self._api_request(method, path, body="" if method == "POST" else None)
                return
            except EgdsError as err:
                last_err = err
                # 404/405/400 — пробуем следующий вариант, остальное — наверх
                if "HTTP 404" not in str(err) and "HTTP 405" not in str(err) and "HTTP 400" not in str(err):
                    raise
        raise last_err or EgdsError("Команда открытия не выполнена")

    # ------------------------------------------------------------------ #
    # Видео
    # ------------------------------------------------------------------ #
    @staticmethod
    def _cam_basic(cam: dict[str, Any]) -> str:
        login = str(cam.get("login") or "")
        password = str(cam.get("password") or "")
        return base64.b64encode(f"{login}:{password}".encode()).decode()

    def _cam_url(self, cam: dict[str, Any], mode: str) -> str:
        host = cam.get("host")
        port = cam.get("port") or (443 if self._video_scheme == "https" else 80)
        guid = cam.get("guid")
        return f"{self._video_scheme}://{host}:{port}/rsapi/{mode}?id={guid}"

    def _cam_ssl(self) -> object:
        """Параметр ssl для запросов к камере (None — проверять, False — нет)."""
        return None if self._video_ssl_verify else False

    def snapshot_url(self, cam: dict[str, Any]) -> str:
        """URL JPEG-кадра камеры."""
        return self._cam_url(cam, "snapshot")

    def stream_url(self, cam: dict[str, Any]) -> str:
        """URL MJPEG-потока камеры с framerate/viewSize."""
        url = self._cam_url(cam, "mjpeg")
        if self._framerate:
            url += f"&framerate={int(self._framerate)}"
        if self._viewsize:
            url += f"&viewSize={self._viewsize}"
        return url

    async def fetch_snapshot(self, cam: dict[str, Any]) -> bytes | None:
        """Получить JPEG-кадр камеры (Basic-авторизация)."""
        url = self.snapshot_url(cam)
        headers = {"Authorization": f"Basic {self._cam_basic(cam)}"}
        try:
            async with self._session.get(
                url,
                headers=headers,
                timeout=ClientTimeout(total=DEFAULT_TIMEOUT),
                ssl=self._cam_ssl(),
            ) as resp:
                if resp.status != 200:
                    warn_limited(
                        _LOGGER,
                        f"snap_http_{url}",
                        "Снимок камеры %s — HTTP %s (кадр не получен)",
                        url,
                        resp.status,
                    )
                    return None
                data = await resp.read()
        except (ClientError, asyncio.TimeoutError, OSError) as err:
            warn_limited(
                _LOGGER,
                f"snap_err_{url}",
                "Снимок камеры %s не удался: %s: %s",
                url,
                type(err).__name__,
                err,
            )
            return None
        if data[:3] == JPEG_START:
            return data
        # иногда сервер отдаёт multipart — вырезаем первый JPEG
        start = data.find(JPEG_START)
        if start >= 0:
            end = data.find(JPEG_END, start)
            if end > 0:
                return data[start : end + 2]
        return None

    async def iter_mjpeg_frames(self, cam: dict[str, Any]):
        """Генератор кадров MJPEG-потока (разбор по JPEG-маркерам).

        Сервер (как и в приложении) шлёт сырые JPEG-кадры подряд либо
        multipart — оба варианта обрабатываются одинаково.
        """
        url = self.stream_url(cam)
        headers = {"Authorization": f"Basic {self._cam_basic(cam)}"}
        buffer = b""
        async with self._session.get(
            url,
            headers=headers,
            timeout=ClientTimeout(total=None, sock_read=30),
            ssl=self._cam_ssl(),
        ) as resp:
            if resp.status != 200:
                raise EgdsError(f"Поток камеры: HTTP {resp.status}")
            async for chunk in resp.content.iter_chunked(65536):
                buffer += chunk
                while True:
                    start = buffer.find(JPEG_START)
                    if start < 0:
                        buffer = buffer[-2:]
                        break
                    end = buffer.find(JPEG_END, start + 3)
                    if end < 0:
                        if start > 0:
                            buffer = buffer[start:]
                        break
                    frame = buffer[start : end + 2]
                    buffer = buffer[end + 2 :]
                    yield frame


def _extract_error(text: str) -> str | None:
    m = re.search(r'"error"\s*:\s*"([^"]+)"', text or "")
    return m.group(1) if m else None

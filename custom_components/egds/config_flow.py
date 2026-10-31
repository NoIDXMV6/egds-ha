"""Процесс добавления интеграции ЕГДС через UI.

Повторяет сценарий входа приложения (OAuth2):
1) POST /oauth/token, grant_type=sms&phone_number={телефон} — SMS-код;
2) POST /oauth/token, grant_type=password&username={телефон}&password={код}
   → access_token + refresh_token (сохраняются в конфиг записи).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import EgdsAuthError, EgdsClient, EgdsDeliveryError, EgdsError, normalize_phone
from .const import (
    COOLDOWN,
    CONF_API_BASE,
    CONF_TOKEN_EXPIRES,
    DEFAULT_COOLDOWN,
    DEFAULT_OPEN_DURATION,
    DEFAULT_OPENING_TIME,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_VIDEO_FRAMERATE,
    DOMAIN,
    OPEN_DURATION,
    OPENING_TIME,
    SCAN_INTERVAL,
    URL_API_BASE,
    VIDEO_FRAMERATE,
    VIDEO_WIDTH,
)

_LOGGER = logging.getLogger(__name__)

# Как включить доставку кода без личного кабинета на сайте (он доступен
# только контактным лицам по договору). Подтверждено по приложению ЕГДС
# 3.3.25: SMS и звонок доступны всем при входе; Telegram и email
# подключаются в самом приложении («Настройки уведомлений»).
DELIVERY_HINT = (
    "Код входа ЕГДС доставляет способом, подключённым для аккаунта. "
    "SMS и звонок (код — последние 4 цифры номера звонившего) работают "
    "у всех при входе. Если код не приходит, подключите другие каналы "
    "БЕЗ сайта — в приложении ЕГДС: «Настройки уведомлений» → подтвердите "
    "email («Запросить подтверждение EMail») и/или Telegram "
    "(подтвердите телефон в боте t.me/egdsk_bot)."
)

STATUS_NO_DELIVERY = (
    "Сервер не выслал код: для аккаунта не подключён ни один способ "
    "доставки. Подключите без сайта, в приложении ЕГДС: «Настройки "
    "уведомлений» → подтвердите email и/или Telegram (бот t.me/egdsk_bot). "
    "Также попробуйте повторить запрос — при входе можно выбрать звонок: "
    "код — последние 4 цифры номера звонившего. Затем повторите запрос кода."
)

STATUS_SMS_BROKEN = (
    "Сервер ЕГДС сейчас не может отправить код (сбой на его стороне). "
    "Повторите запрос позже — кнопкой «Выслать код повторно» либо "
    "заново добавив интеграцию."
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Optional(CONF_API_BASE, default=URL_API_BASE): str,
    }
)

STEP_CODE_SCHEMA = vol.Schema(
    {
        vol.Required("code"): str,
        vol.Required("resend", default=False): bool,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(SCAN_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=60, max=3600)
        ),
        vol.Required(OPENING_TIME, default=DEFAULT_OPENING_TIME): vol.All(
            vol.Coerce(int), vol.Range(min=3, max=60)
        ),
        vol.Required(OPEN_DURATION, default=DEFAULT_OPEN_DURATION): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=120)
        ),
        vol.Required(COOLDOWN, default=DEFAULT_COOLDOWN): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=120)
        ),
        vol.Required(VIDEO_FRAMERATE, default=DEFAULT_VIDEO_FRAMERATE): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=10)
        ),
        vol.Optional(VIDEO_WIDTH, default=""): str,
    }
)


def _make_client(hass, data: dict[str, Any]) -> EgdsClient:
    session = async_get_clientsession(hass)
    return EgdsClient(
        session,
        phone=data[CONF_USERNAME],
        api_base=data.get(CONF_API_BASE) or URL_API_BASE,
    )


class EgdsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Обработка config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Зависшие попытки настройки
    # ------------------------------------------------------------------ #
    @callback
    def _async_abort_stale_flows(self, unique_id: str) -> None:
        """Прервать зависшие потоки настройки с тем же аккаунтом.

        Незавершённый flow (закрыли окно с кодом, код не пришёл, HA
        перезапускали) остаётся «в работе» и переживает рестарты Home
        Assistant. Пока он жив, каждая новая попытка добавления
        обрывалась с already_in_progress. Прерываем старые потоки,
        чтобы настройку можно было начать заново.
        """
        flow_mgr = self.hass.config_entries.flow
        for progress in flow_mgr.async_progress_by_handler(DOMAIN):
            if isinstance(progress, dict):
                flow_id = progress.get("flow_id")
                ctx = progress.get("context") or {}
            else:  # на случай, если в будущих HA flow станет объектом
                flow_id = getattr(progress, "flow_id", None)
                ctx = getattr(progress, "context", None) or {}
            if not flow_id or flow_id == getattr(self, "flow_id", None):
                continue
            if ctx.get("unique_id") == unique_id:
                _LOGGER.debug("Прерываю зависший flow настройки %s", flow_id)
                flow_mgr.async_abort(flow_id)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        status = DELIVERY_HINT
        if user_input is not None:
            phone = normalize_phone(user_input[CONF_USERNAME])
            # Сначала убираем зависшие попытки настройки этого же аккаунта,
            # затем привязываем flow: иначе async_set_unique_id обрывает
            # поток с already_in_progress.
            self._async_abort_stale_flows(phone)
            await self.async_set_unique_id(phone, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self.data = dict(user_input)
            self.data[CONF_USERNAME] = phone
            client = _make_client(self.hass, self.data)
            try:
                await client.request_sms_code()
            except EgdsDeliveryError as err:
                errors["base"] = "delivery_unavailable"
                status = STATUS_NO_DELIVERY
                _LOGGER.debug("Нет каналов доставки: %s", err)
            except EgdsAuthError as err:
                errors["base"] = "sms_failed"
                # например: «Сбой в работе сервера, попробуйте еще раз позже»
                status = STATUS_SMS_BROKEN + f" Ответ сервера: {err}"
                _LOGGER.debug("SMS не отправлено: %s", err)
            except EgdsError as err:
                errors["base"] = "cannot_connect"
                status = f"Ошибка связи: {err}"
                _LOGGER.debug("Ошибка связи: %s", err)
            else:
                self.data["code_status"] = (
                    "Код отправлен (SMS, звонок, бот или email — как настроено "
                    "для аккаунта; код звонка — последние 4 цифры номера "
                    "звонившего). Введите его ниже."
                )
                return await self.async_step_code()
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors or None,
            description_placeholders={"status": status},
        )

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        status = str(
            self.data.get("code_status")
            or "Введите код. Если код не пришёл — отметьте «Выслать код повторно»."
        )
        if user_input is not None:
            if user_input.get("resend"):
                client = _make_client(self.hass, self.data)
                try:
                    await client.request_sms_code()
                except EgdsDeliveryError as err:
                    errors["base"] = "delivery_unavailable"
                    status = STATUS_NO_DELIVERY
                except EgdsAuthError as err:
                    errors["base"] = "sms_failed"
                    status = STATUS_SMS_BROKEN + f" Ответ сервера: {err}"
                except EgdsError as err:
                    errors["base"] = "cannot_connect"
                    status = f"Ошибка связи: {err}"
                else:
                    status = "Код выслан повторно."
            if "base" not in errors:
                client = _make_client(self.hass, self.data)
                try:
                    await client.login_with_code(user_input["code"])
                except EgdsAuthError as err:
                    errors["base"] = "invalid_code"
                    status = f"Сервер отклонил код: {err}"
                except EgdsError as err:
                    errors["base"] = "cannot_connect"
                    status = f"Ошибка связи: {err}"
                else:
                    self.data["access_token"] = client.access_token
                    self.data["refresh_token"] = client.refresh_token
                    if client.token_expires is not None:
                        self.data[CONF_TOKEN_EXPIRES] = client.token_expires
                    # аккаунт могли настроить из другого окна, пока ждали код
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title="ЕГДС", data=self.data)
        self.data["code_status"] = status
        return self.async_show_form(
            step_id="code",
            data_schema=STEP_CODE_SCHEMA,
            errors=errors or None,
            description_placeholders={"status": status},
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self.data = dict(entry_data)
        # Сразу высылаем новый код (как при первом входе) — пользователю
        # остаётся только ввести его. Если доставка не настроена — покажем
        # инструкцию вместо формы.
        client = _make_client(self.hass, self.data)
        try:
            await client.request_sms_code()
            self.data["code_status"] = (
                "Новый код отправлен (SMS, звонок, бот или email — как "
                "настроено для аккаунта; код звонка — последние 4 цифры "
                "номера звонившего). Введите его ниже."
            )
        except EgdsDeliveryError:
            self.data["code_status"] = STATUS_NO_DELIVERY
        except EgdsError as err:
            self.data["code_status"] = (
                f"Не удалось выслать код автоматически: {err}. "
                "Отметьте «Выслать код повторно» и попробуйте ещё раз."
            )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Reauth: ввести код (запрошен автоматически при старте)."""
        errors: dict[str, str] = {}
        status = str(
            self.data.get("code_status")
            or "Сессия истекла. Введите новый код доставки."
        )
        if user_input is not None:
            client = _make_client(self.hass, self.data)
            if user_input.get("resend"):
                try:
                    await client.request_sms_code()
                except EgdsDeliveryError as err:
                    errors["base"] = "delivery_unavailable"
                    status = STATUS_NO_DELIVERY
                except EgdsAuthError as err:
                    errors["base"] = "sms_failed"
                    status = STATUS_SMS_BROKEN + f" Ответ сервера: {err}"
                except EgdsError as err:
                    errors["base"] = "cannot_connect"
                    status = f"Ошибка связи: {err}"
                else:
                    status = "Код выслан повторно."
            if "base" not in errors:
                try:
                    await client.login_with_code(user_input["code"])
                except EgdsAuthError as err:
                    errors["base"] = "invalid_code"
                    status = f"Сервер отклонил код: {err}"
                except EgdsError as err:
                    errors["base"] = "cannot_connect"
                    status = f"Ошибка связи: {err}"
                else:
                    self.data["access_token"] = client.access_token
                    self.data["refresh_token"] = client.refresh_token
                    if client.token_expires is not None:
                        self.data[CONF_TOKEN_EXPIRES] = client.token_expires
                    entry = self._get_reauth_entry()
                    self.hass.config_entries.async_update_entry(entry, data=self.data)
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_CODE_SCHEMA,
            errors=errors or None,
            description_placeholders={"status": status},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return EgdsOptionsFlow()


class EgdsOptionsFlow(OptionsFlow):
    """Меню: параметры опроса и повторная авторизация без удаления."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Меню настроек интеграции."""
        return self.async_show_menu(step_id="init", menu_options=["options", "reauth"])

    async def async_step_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Параметры опроса, таймингов и видео."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options = self.config_entry.options
        defaults = {
            SCAN_INTERVAL: options.get(SCAN_INTERVAL, DEFAULT_POLL_INTERVAL),
            OPENING_TIME: options.get(OPENING_TIME, DEFAULT_OPENING_TIME),
            OPEN_DURATION: options.get(OPEN_DURATION, DEFAULT_OPEN_DURATION),
            COOLDOWN: options.get(COOLDOWN, DEFAULT_COOLDOWN),
            VIDEO_FRAMERATE: options.get(VIDEO_FRAMERATE, DEFAULT_VIDEO_FRAMERATE),
            VIDEO_WIDTH: options.get(VIDEO_WIDTH, ""),
        }
        return self.async_show_form(
            step_id="options",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, defaults),
        )

    async def async_step_reauth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Запустить повторную авторизацию без удаления интеграции."""
        entry = self.config_entry
        try:
            # HA ≥ 2024.2: штатный запуск reauth-flow (с дедупликацией)
            entry.async_start_reauth(self.hass)
        except AttributeError:
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={
                        "source": "reauth",
                        "entry_id": entry.entry_id,
                        "unique_id": entry.unique_id,
                        "title_placeholders": {},
                    },
                    data=entry.data,
                )
            )
        return self.async_abort(reason="reauth_started")

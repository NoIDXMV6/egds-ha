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

from .api import EgdsAuthError, EgdsClient, EgdsError, normalize_phone
from .const import (
    COOLDOWN,
    CONF_API_BASE,
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

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = normalize_phone(user_input[CONF_USERNAME])
            await self.async_set_unique_id(phone)
            self._abort_if_unique_id_configured()
            self.data = dict(user_input)
            self.data[CONF_USERNAME] = phone
            client = _make_client(self.hass, self.data)
            try:
                await client.request_sms_code()
            except EgdsAuthError as err:
                errors["base"] = "sms_failed"
                _LOGGER.debug("SMS не отправлено: %s", err)
            except EgdsError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.debug("Ошибка связи: %s", err)
            else:
                return await self.async_step_code()
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("resend"):
                client = _make_client(self.hass, self.data)
                try:
                    await client.request_sms_code()
                except EgdsError:
                    errors["base"] = "sms_failed"
            if "base" not in errors:
                client = _make_client(self.hass, self.data)
                try:
                    await client.login_with_code(user_input["code"])
                except EgdsAuthError:
                    errors["base"] = "invalid_code"
                except EgdsError:
                    errors["base"] = "cannot_connect"
                else:
                    self.data["access_token"] = client.access_token
                    self.data["refresh_token"] = client.refresh_token
                    return self.async_create_entry(title="ЕГДС", data=self.data)
        return self.async_show_form(step_id="code", data_schema=STEP_CODE_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self.data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Reauth: запросить новый SMS-код и войти заново."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = _make_client(self.hass, self.data)
            if user_input.get("resend"):
                try:
                    await client.request_sms_code()
                except EgdsError:
                    errors["base"] = "sms_failed"
            if "base" not in errors:
                try:
                    await client.login_with_code(user_input["code"])
                except EgdsAuthError:
                    errors["base"] = "invalid_code"
                except EgdsError:
                    errors["base"] = "cannot_connect"
                else:
                    self.data["access_token"] = client.access_token
                    self.data["refresh_token"] = client.refresh_token
                    entry = self._get_reauth_entry()
                    self.hass.config_entries.async_update_entry(entry, data=self.data)
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")
        return self.async_show_form(step_id="reauth_confirm", data_schema=STEP_CODE_SCHEMA, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return EgdsOptionsFlow()


class EgdsOptionsFlow(OptionsFlow):
    """Настройки опроса, таймингов и видео."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, defaults),
        )

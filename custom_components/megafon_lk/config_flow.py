"""Config flow: phone number -> SMS code, with a cookie+headers fallback."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    MegafonAuthError,
    MegafonCaptchaError,
    MegafonClient,
    MegafonError,
    MegafonOtpSession,
    async_create_session,
)
from .const import (
    CONF_CABINET_PARAMS,
    CONF_CHECK_INFO,
    CONF_COOKIE,
    CONF_ID_PARAM,
    CONF_PHONE,
    CONF_SCAN_INTERVAL,
    CONF_VALIDATION_PARAM,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MIN_SCAN_INTERVAL_MINUTES,
)

_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_MULTILINE = TextSelector(TextSelectorConfig(multiline=True))

# Accepts a pasted cURL command or just the three -H 'x-cabinet-*: <uuid>' lines.
_CABINET_PARAM_RE = re.compile(
    r"x-cabinet-(check-info|id-param|validation-param)[\s\"':=]*"
    r"([0-9a-f]{4,12}(?:-[0-9a-f]{4,16}){3,4})",
    re.IGNORECASE,
)
_CABINET_HEADER_KEYS = {
    "check-info": CONF_CHECK_INFO,
    "id-param": CONF_ID_PARAM,
    "validation-param": CONF_VALIDATION_PARAM,
}


def _parse_cabinet_params(raw: str) -> dict[str, str]:
    """Pull the three x-cabinet-* UUIDs out of a pasted cURL/headers blob."""
    return {
        _CABINET_HEADER_KEYS[m.group(1).lower()]: m.group(2)
        for m in _CABINET_PARAM_RE.finditer(raw)
    }


STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PHONE): str,
        vol.Required(CONF_CABINET_PARAMS): _MULTILINE,
    }
)
STEP_OTP_SCHEMA = vol.Schema({vol.Required("otp"): str})
STEP_COOKIE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_COOKIE): _SECRET,
        vol.Required(CONF_CHECK_INFO): _SECRET,
        vol.Required(CONF_ID_PARAM): _SECRET,
        vol.Required(CONF_VALIDATION_PARAM): _SECRET,
    }
)

_SCAN_INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL_MINUTES,
        max=1440,
        step=1,
        unit_of_measurement="min",
        mode=NumberSelectorMode.BOX,
    )
)


class MegafonConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._session = None
        self._otp: MegafonOtpSession | None = None
        self._phone = ""
        self._cabinet: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MegafonOptionsFlow:
        return MegafonOptionsFlow(config_entry)

    def _client_data_from_otp(self) -> dict[str, str]:
        """Cookie + the user's x-cabinet-* values for MegafonClient kwargs."""
        return {
            CONF_COOKIE: self._otp.cookie_header,
            CONF_CHECK_INFO: self._cabinet.get(CONF_CHECK_INFO, ""),
            CONF_ID_PARAM: self._cabinet.get(CONF_ID_PARAM, ""),
            CONF_VALIDATION_PARAM: self._cabinet.get(CONF_VALIDATION_PARAM, ""),
        }

    async def _validate_cookie(self, data: dict[str, Any]) -> tuple[str | None, str | None]:
        session = async_create_session(self.hass)
        client = MegafonClient(session, **data)
        try:
            await client.async_get_balance()
        except MegafonAuthError:
            return None, "invalid_auth"
        except MegafonError:
            return None, "cannot_connect"
        finally:
            await session.close()
        return client.msisdn or self._phone or "megafon", None

    async def _entry_data(self, data: dict[str, str]) -> tuple[str | None, str | None]:
        """Validate harvested creds; fall back to the cookie step on failure."""
        msisdn, error = await self._validate_cookie(
            {k: data.get(k, "") for k in (CONF_COOKIE, CONF_CHECK_INFO, CONF_ID_PARAM, CONF_VALIDATION_PARAM)}
        )
        return msisdn, error

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cabinet = _parse_cabinet_params(user_input[CONF_CABINET_PARAMS])
            if len(cabinet) < 3:
                errors[CONF_CABINET_PARAMS] = "invalid_headers"
                return self.async_show_form(
                    step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
                )
            self._cabinet = cabinet
            self._phone = user_input[CONF_PHONE].strip()
            self._session = async_create_session(self.hass)
            self._otp = MegafonOtpSession(
                self._session,
                cabinet_headers={
                    "x-cabinet-check-info": cabinet[CONF_CHECK_INFO],
                    "x-cabinet-id-param": cabinet[CONF_ID_PARAM],
                    "x-cabinet-validation-param": cabinet[CONF_VALIDATION_PARAM],
                },
            )
            try:
                await self._otp.async_bootstrap()
                await self._otp.async_request_otp(self._phone)
            except MegafonCaptchaError:
                return await self.async_step_cookie()
            except MegafonAuthError:
                errors["base"] = "invalid_phone"
            except (MegafonError, Exception):
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_otp()
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._otp.async_submit_otp(self._phone, user_input["otp"].strip())
            except MegafonAuthError:
                errors["base"] = "invalid_otp"
            except MegafonCaptchaError:
                return await self.async_step_cookie()
            except MegafonError:
                errors["base"] = "cannot_connect"
            else:
                data = self._client_data_from_otp()
                msisdn, error = await self._entry_data(data)
                if error is not None:
                    # SMS login worked but data calls need more — fall back.
                    self._pending_otp_data = data
                    return await self.async_step_cookie()
                await self.async_set_unique_id(msisdn)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"МегаФон …{msisdn[-4:]}",
                    data={CONF_PHONE: self._phone, **data},
                )
        return self.async_show_form(
            step_id="otp", data_schema=STEP_OTP_SCHEMA, errors=errors
        )

    async def async_step_cookie(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fallback: paste cookie + x-cabinet-* headers from the browser."""
        errors: dict[str, str] = {}
        if user_input is not None:
            msisdn, error = await self._validate_cookie(user_input)
            if error is not None:
                errors["base"] = error
            else:
                await self.async_set_unique_id(msisdn)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"МегаФон …{msisdn[-4:]}",
                    data={CONF_PHONE: self._phone, **user_input},
                )
        return self.async_show_form(
            step_id="cookie", data_schema=STEP_COOKIE_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before sending an SMS — requesting a code on every dialog
        open would spam the user's phone."""
        entry = self._get_reauth_entry()
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=vol.Schema({})
            )
        self._phone = entry.data.get(CONF_PHONE, "")
        self._session = async_create_session(self.hass)
        self._otp = MegafonOtpSession(
            self._session,
            cabinet_headers={
                "x-cabinet-check-info": entry.data.get(CONF_CHECK_INFO, ""),
                "x-cabinet-id-param": entry.data.get(CONF_ID_PARAM, ""),
                "x-cabinet-validation-param": entry.data.get(
                    CONF_VALIDATION_PARAM, ""
                ),
            },
        )
        try:
            await self._otp.async_bootstrap()
            await self._otp.async_request_otp(self._phone)
        except (MegafonError, Exception):
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
                errors={"base": "cannot_connect"},
            )
        return await self.async_step_reauth_otp()

    async def async_step_reauth_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fresh SMS code for the stored phone."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._otp.async_submit_otp(
                    self._phone, user_input["otp"].strip()
                )
            except MegafonAuthError:
                errors["base"] = "invalid_otp"
            except MegafonError:
                errors["base"] = "cannot_connect"
            else:
                data = self._client_data_from_otp()
                # The x-cabinet params live in the entry already — keep them.
                for key in (CONF_CHECK_INFO, CONF_ID_PARAM, CONF_VALIDATION_PARAM):
                    if entry.data.get(key):
                        data[key] = entry.data[key]
                return self.async_update_reload_and_abort(
                    entry, data_updates=data
                )
        return self.async_show_form(
            step_id="reauth_otp", data_schema=STEP_OTP_SCHEMA, errors=errors
        )


class MegafonOptionsFlow(OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self._config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_SCAN_INTERVAL, default=current): _SCAN_INTERVAL_SELECTOR}
            ),
        )

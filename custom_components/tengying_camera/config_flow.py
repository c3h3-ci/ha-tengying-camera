"""Config flow for Tengying Smart Camera integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv

from .api import YingTengApi, YingTengAuthError
from .const import CONF_RTSP_HOST, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_NAME, default="影腾智联"): str,
        vol.Optional(
            CONF_RTSP_HOST,
            default="",
        ): str,
    }
)


class TengyingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tengying Camera."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            name = user_input.get(CONF_NAME, "影腾智联")

            api = YingTengApi(
                async_get_clientsession(self.hass),
                username=username,
                password=password,
            )
            try:
                await api.login(username, password)
                user_info = await api.get_user_info()
                devices = await api.get_device_list()
            except YingTengAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"tengying_{api._user_id}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        "user_id": api._user_id,
                        CONF_RTSP_HOST: user_input.get(CONF_RTSP_HOST, ""),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

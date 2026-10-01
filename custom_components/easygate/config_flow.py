"""Config flow for the 2N EasyGate integration."""

from __future__ import annotations

import logging
import os
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .at import EasyGateBusy, EasyGateClient, EasyGateError
from .const import (
    BAUDRATES,
    CONF_BAUDRATE,
    CONF_COMMAND_NUMBERS,
    CONF_COMMAND_PIN,
    CONF_DAILY_LIMIT,
    CONF_DELETE_SMS,
    CONF_MAX_PARTS,
    CONF_MISSED_CALLS,
    CONF_PORT,
    CONF_RECIPIENTS,
    CONF_RTSCTS,
    CONF_SCAN_INTERVAL,
    CONF_STRIP_DIACRITICS,
    DEFAULT_BAUDRATE,
    DEFAULT_DAILY_LIMIT,
    DEFAULT_MAX_PARTS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _list_ports() -> list[str]:
    from serial.tools import list_ports  # pylint: disable=import-outside-toplevel

    ports: list[str] = []
    for folder in ("/dev/serial/by-id", "/dev/serial/by-path"):
        if os.path.isdir(folder):
            ports += [os.path.join(folder, n) for n in sorted(os.listdir(folder))]
    ports += [p.device for p in list_ports.comports()]
    return ports


def _test(port: str, baudrate: int, rtscts: bool) -> dict[str, str]:
    client = EasyGateClient(port, baudrate=baudrate, rtscts=rtscts)
    try:
        client.ping()
        return client.identity()
    finally:
        client.close()


class EasyGateConfigFlow(ConfigFlow, domain=DOMAIN):
    """Serial port setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            port = user_input[CONF_PORT]
            baudrate = int(user_input[CONF_BAUDRATE])
            await self.async_set_unique_id(port)
            self._abort_if_unique_id_configured()
            try:
                identity = await self.hass.async_add_executor_job(
                    _test, port, baudrate, user_input[CONF_RTSCTS]
                )
            except EasyGateBusy:
                errors["base"] = "busy"
            except EasyGateError as err:
                _LOGGER.warning("EasyGate on %s does not answer: %s", port, err)
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("unexpected error testing %s", port)
                errors["base"] = "unknown"
            else:
                _LOGGER.info("EasyGate found on %s: %s", port, identity)
                return self.async_create_entry(
                    title="2N EasyGate",
                    data={
                        CONF_PORT: port,
                        CONF_BAUDRATE: baudrate,
                        CONF_RTSCTS: user_input[CONF_RTSCTS],
                    },
                )

        ports = await self.hass.async_add_executor_job(_list_ports)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PORT, default=ports[0] if ports else "/dev/ttyUSB1"
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=ports, custom_value=True, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Required(CONF_BAUDRATE, default=str(DEFAULT_BAUDRATE)): SelectSelector(
                    SelectSelectorConfig(options=[str(b) for b in BAUDRATES])
                ),
                vol.Required(CONF_RTSCTS, default=True): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return EasyGateOptionsFlow()


class EasyGateOptionsFlow(OptionsFlow):
    """Recipients, SMS limits, polling."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            for key in (CONF_DAILY_LIMIT, CONF_MAX_PARTS, CONF_SCAN_INTERVAL):
                user_input[key] = int(user_input[key])
            return self.async_create_entry(data=user_input)

        o = self.config_entry.options

        def number(minimum: int, maximum: int, unit: str | None = None) -> NumberSelector:
            config = NumberSelectorConfig(
                min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX
            )
            if unit:  # the selector rejects unit_of_measurement=None
                config["unit_of_measurement"] = unit
            return NumberSelector(config)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_RECIPIENTS, default=o.get(CONF_RECIPIENTS, "")
                ): TextSelector(),
                vol.Required(
                    CONF_DAILY_LIMIT, default=o.get(CONF_DAILY_LIMIT, DEFAULT_DAILY_LIMIT)
                ): number(0, 100, "SMS"),
                vol.Required(
                    CONF_MAX_PARTS, default=o.get(CONF_MAX_PARTS, DEFAULT_MAX_PARTS)
                ): number(1, 5),
                vol.Required(
                    CONF_STRIP_DIACRITICS, default=o.get(CONF_STRIP_DIACRITICS, True)
                ): BooleanSelector(),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=o.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): number(5, 300, "s"),
                vol.Required(
                    CONF_MISSED_CALLS, default=o.get(CONF_MISSED_CALLS, True)
                ): BooleanSelector(),
                vol.Required(
                    CONF_DELETE_SMS, default=o.get(CONF_DELETE_SMS, True)
                ): BooleanSelector(),
                vol.Optional(
                    CONF_COMMAND_NUMBERS, default=o.get(CONF_COMMAND_NUMBERS, "")
                ): TextSelector(),
                vol.Optional(
                    CONF_COMMAND_PIN, default=o.get(CONF_COMMAND_PIN, "")
                ): TextSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

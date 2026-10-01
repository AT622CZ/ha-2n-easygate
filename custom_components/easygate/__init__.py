"""2N EasyGate GSM gateway integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .at import EasyGateClient, EasyGateError
from .const import (
    CONF_BAUDRATE,
    CONF_PORT,
    CONF_RTSCTS,
    DEFAULT_BAUDRATE,
    DOMAIN,
    SERVICE_AT_COMMAND,
    SERVICE_SEND_SMS,
)
from .coordinator import EasyGateCoordinator, split_recipients

PLATFORMS = [Platform.EVENT, Platform.NOTIFY, Platform.SENSOR]

type EasyGateConfigEntry = ConfigEntry[EasyGateCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SEND_SMS_SCHEMA = vol.Schema(
    {
        vol.Required("message"): cv.string,
        vol.Optional("number"): vol.Any(cv.string, [cv.string]),
        vol.Optional("force", default=False): cv.boolean,
    }
)
AT_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required("command"): vol.All(cv.string, vol.Match(r"^(?i:AT).*")),
        vol.Optional("timeout", default=10): vol.All(vol.Coerce(float), vol.Range(1, 60)),
    }
)


def _coordinator(hass: HomeAssistant) -> EasyGateCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError("EasyGate is not loaded")
    return entries[0].runtime_data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services."""

    async def send_sms(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        sent = await coordinator.async_send_sms(
            call.data["message"],
            split_recipients(call.data.get("number")),
            call.data["force"],
        )
        return {"parts_sent": sent, "sent_today": coordinator.data.sms_sent_today}

    async def at_command(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        try:
            lines = await hass.async_add_executor_job(
                coordinator.client.command, call.data["command"], call.data["timeout"]
            )
        except EasyGateError as err:
            raise HomeAssistantError(str(err)) from err
        return {"response": lines}

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_SMS,
        send_sms,
        schema=SEND_SMS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_AT_COMMAND,
        at_command,
        schema=AT_COMMAND_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EasyGateConfigEntry) -> bool:
    client = EasyGateClient(
        entry.data[CONF_PORT],
        baudrate=entry.data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
        rtscts=entry.data.get(CONF_RTSCTS, True),
    )
    coordinator = EasyGateCoordinator(hass, entry, client)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: EasyGateConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EasyGateConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await hass.async_add_executor_job(entry.runtime_data.client.close)
    return unloaded

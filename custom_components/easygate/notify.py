"""SMS notify entity of the 2N EasyGate gateway."""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EasyGateConfigEntry
from .coordinator import EasyGateCoordinator
from .entity import EasyGateEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EasyGateConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([SmsNotify(entry.runtime_data)])


class SmsNotify(EasyGateEntity, NotifyEntity):
    """notify.send_message -> SMS to the recipients set in the options."""

    _attr_icon = "mdi:message-alert"
    _attr_name = "SMS"

    def __init__(self, coordinator: EasyGateCoordinator) -> None:
        super().__init__(coordinator, "sms")

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        text = f"{title}: {message}" if title else message
        await self.coordinator.async_send_sms(text)

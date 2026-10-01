"""Event entities: make the integration's events visible on the device page."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EasyGateConfigEntry
from .const import EVENT_CALL, EVENT_COMMAND, EVENT_MISSED_CALL, EVENT_SMS
from .coordinator import EasyGateCoordinator
from .entity import EasyGateEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EasyGateConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    c = entry.runtime_data
    async_add_entities(
        [
            BusEvent(c, "event_sms", "Přijatá SMS", "mdi:message-text", EVENT_SMS),
            BusEvent(c, "event_command", "SMS příkaz", "mdi:message-cog", EVENT_COMMAND),
            BusEvent(c, "event_call", "Příchozí hovor", "mdi:phone-incoming", EVENT_CALL),
            BusEvent(c, "event_missed", "Zmeškaný hovor", "mdi:phone-missed",
                     EVENT_MISSED_CALL),
        ]  # fmt: skip
    )


class BusEvent(EasyGateEntity, EventEntity):
    """Mirrors one bus event (same name and data) as an entity."""

    def __init__(
        self, coordinator: EasyGateCoordinator, key: str, name: str, icon: str, event: str
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._attr_icon = icon
        self._attr_event_types = [event]
        self._event = event

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self.coordinator.signal, self._handle)
        )

    @callback
    def _handle(self, event_type: str, data: dict) -> None:
        if event_type == self._event:
            self._trigger_event(event_type, data)
            self.async_write_ha_state()

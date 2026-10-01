"""Sensors of the 2N EasyGate GSM gateway."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EasyGateConfigEntry
from .const import REGISTRATION_NAMES
from .coordinator import EasyGateCoordinator, EasyGateData
from .entity import EasyGateEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EasyGateConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    c = entry.runtime_data
    async_add_entities(
        [
            SignalSensor(c),
            OperatorSensor(c),
            RegistrationSensor(c),
            CallSensor(c),
            LastSmsSensor(c),
            LastSmsFromSensor(c),
            LastMissedCallSensor(c),
            SmsSentTodaySensor(c),
            SmsStoredSensor(c),
        ]
    )


class _Sensor(EasyGateEntity, SensorEntity):
    def __init__(self, coordinator: EasyGateCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name

    @property
    def _data(self) -> EasyGateData:
        return self.coordinator.data


class SignalSensor(_Sensor):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EasyGateCoordinator) -> None:
        super().__init__(coordinator, "signal", "Signál GSM")

    @property
    def native_value(self) -> int | None:
        return self._data.signal_dbm

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"bit_error_rate": self._data.bit_error_rate}


class OperatorSensor(_Sensor):
    _attr_icon = "mdi:sim"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EasyGateCoordinator) -> None:
        super().__init__(coordinator, "operator", "Operátor")

    @property
    def native_value(self) -> str | None:
        return self._data.operator


class RegistrationSensor(_Sensor):
    _attr_icon = "mdi:signal-cellular-3"

    def __init__(self, coordinator: EasyGateCoordinator) -> None:
        super().__init__(coordinator, "registration", "Registrace v síti")

    @property
    def native_value(self) -> str | None:
        reg = self._data.registration
        return REGISTRATION_NAMES.get(reg, reg) if reg else None


class CallSensor(_Sensor):
    """Current call over GSM."""

    def __init__(self, coordinator: EasyGateCoordinator) -> None:
        super().__init__(coordinator, "call", "Hovor GSM")

    @property
    def native_value(self) -> str:
        data = self._data
        if data.calls:
            call = data.calls[0]
            direction = "příchozí" if call.incoming else "odchozí"
            names = {"active": "hovor", "dialing": "vytáčení", "alerting": "vyzvání",
                     "incoming": "zvoní", "held": "přidržen", "waiting": "čeká"}  # fmt: skip
            return f"{direction} – {names.get(call.state_name, call.state_name)}"
        if data.ringing:
            return "příchozí – zvoní"
        # The gateway also blocks the port for its own housekeeping, so a
        # blocked port alone is not reported as a call (see port_blocked).
        return "klid"

    @property
    def icon(self) -> str:
        if self._data.ringing:
            return "mdi:phone-ring"
        return "mdi:phone-in-talk" if self._data.calls else "mdi:phone"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._data
        call = data.calls[0] if data.calls else None
        return {
            "number": call.number if call else None,
            "direction": ("incoming" if call.incoming else "outgoing") if call else None,
            "port_blocked": data.busy,
            "last_caller": data.last_caller,
        }


class LastSmsSensor(_Sensor):
    _attr_icon = "mdi:message-text"

    def __init__(self, coordinator: EasyGateCoordinator) -> None:
        super().__init__(coordinator, "last_sms", "Poslední přijatá SMS")

    @property
    def native_value(self) -> str | None:
        sms = self._data.last_sms
        return sms.text[:255] if sms else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        sms = self._data.last_sms
        return sms.as_event() if sms else None


class LastSmsFromSensor(_Sensor):
    _attr_icon = "mdi:message-reply-text"

    def __init__(self, coordinator: EasyGateCoordinator) -> None:
        super().__init__(coordinator, "last_sms_from", "Poslední SMS od")

    @property
    def native_value(self) -> str | None:
        sms = self._data.last_sms
        return sms.number if sms else None


class LastMissedCallSensor(_Sensor):
    _attr_icon = "mdi:phone-missed"

    def __init__(self, coordinator: EasyGateCoordinator) -> None:
        super().__init__(coordinator, "last_missed", "Poslední zmeškaný hovor")

    @property
    def native_value(self) -> str | None:
        return self._data.last_missed

    @property
    def extra_state_attributes(self) -> dict[str, datetime | None]:
        return {"detected": self._data.last_missed_at}


class SmsSentTodaySensor(_Sensor):
    _attr_icon = "mdi:message-arrow-right"
    _attr_native_unit_of_measurement = "SMS"

    def __init__(self, coordinator: EasyGateCoordinator) -> None:
        super().__init__(coordinator, "sms_sent_today", "SMS odeslané dnes")

    @property
    def native_value(self) -> int:
        return self._data.sms_sent_today

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"daily_limit": self.coordinator.daily_limit}


class SmsStoredSensor(_Sensor):
    """Messages stored in the gateway; when full, no SMS can be received."""

    _attr_icon = "mdi:message-badge"
    _attr_native_unit_of_measurement = "SMS"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EasyGateCoordinator) -> None:
        super().__init__(coordinator, "sms_stored", "SMS v paměti brány")

    @property
    def native_value(self) -> int | None:
        return self._data.sms_stored

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"capacity": self._data.sms_capacity}

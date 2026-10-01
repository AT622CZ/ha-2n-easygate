"""Base entity for the 2N EasyGate integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import EasyGateCoordinator


class EasyGateEntity(CoordinatorEntity[EasyGateCoordinator]):
    """Entity attached to the gateway device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EasyGateCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        identity = coordinator.identity
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            model=f"{MODEL} ({identity['model']})" if identity.get("model") else MODEL,
            name=f"{MANUFACTURER} {MODEL}",
            sw_version=identity.get("revision"),
            serial_number=identity.get("imei"),
        )

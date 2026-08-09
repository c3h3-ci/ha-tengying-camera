"""Platform for sensor entities for Tengying Smart Camera."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import TengyingDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator: TengyingDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]

    entities = []
    for device in coordinator.data.get("devices", []):
        uuid = device["uuid"]
        name = device.get("name", uuid)
        entities.append(TengyingDeviceSensor(coordinator, uuid, name))
        entities.append(TengyingAlarmSensor(coordinator, uuid, name))

    async_add_entities(entities)


class TengyingDeviceSensor(CoordinatorEntity, SensorEntity):
    """Sensor for a Tengying camera device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TengyingDataUpdateCoordinator,
        uuid: str,
        name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._uuid = uuid
        self._device_name = name
        self._attr_unique_id = f"tengying_{uuid}"
        self._attr_icon = "mdi:camera"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for device registry."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._device_name,
            manufacturer=MANUFACTURER,
            model="智能摄像头",
        )

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{self._device_name}"

    @property
    def state(self) -> str:
        """Return the state - device status."""
        device_map = self.coordinator.data.get("device_map", {})
        device = device_map.get(self._uuid, {})
        # Device is considered online if present in the list
        return "在线" if device else "离线"

    @property
    def extra_state_attributes(self) -> dict:
        """Return extra state attributes."""
        device_map = self.coordinator.data.get("device_map", {})
        device = device_map.get(self._uuid, {})
        return {
            "uuid": self._uuid,
            "device_id": device.get("device_id", ""),
        }


class TengyingAlarmSensor(CoordinatorEntity, SensorEntity):
    """最新告警消息传感器（motion/body/car/pet/sound）。"""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TengyingDataUpdateCoordinator,
        uuid: str,
        name: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._uuid = uuid
        self._device_name = name
        self._attr_unique_id = f"tengying_{uuid}_alarm"
        self._attr_icon = "mdi:alert-circle"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for device registry."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._device_name,
            manufacturer=MANUFACTURER,
            model="智能摄像头",
        )

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"{self._device_name} 最新告警"

    @property
    def state(self) -> str:
        """Return the state - latest alarm tag name."""
        alarm = self.coordinator.data.get("alarms", {}).get(self._uuid, {})
        tag = alarm.get("tag") or {}
        return tag.get("name") or "无"

    @property
    def extra_state_attributes(self) -> dict:
        """Return extra state attributes."""
        alarm = self.coordinator.data.get("alarms", {}).get(self._uuid, {})
        tag = alarm.get("tag") or {}
        return {
            "uuid": self._uuid,
            "alarm_id": alarm.get("id", ""),
            "tag": tag.get("tag", ""),
            "time": alarm.get("time"),
            "thumbnail": alarm.get("thumbnail", ""),
            "can_play": alarm.get("can_play"),
        }

"""Platform for camera entities for Tengying Smart Camera."""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import YingTengApi
from .const import DOMAIN, MANUFACTURER
from .coordinator import TengyingDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the camera platform."""
    coordinator: TengyingDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    api: YingTengApi = hass.data[DOMAIN][entry.entry_id]["api"]

    entities = []
    for device in coordinator.data.get("devices", []):
        uuid = device["uuid"]
        name = device.get("name", uuid)
        entities.append(TengyingCamera(coordinator, api, uuid, name))

    async_add_entities(entities)


class TengyingCamera(Camera):
    """Camera entity for a Tengying device.

    Tengying cameras stream over TUTK/Kalay P2P. We run a separate
    ``tutk-bridge`` addon that connects with the device UID + avPass (reverse-
    engineered from the app) and republishes the H.264 feed as RTSP. When the
    bridge host is configured, this entity exposes a live RTSP ``stream_source``;
    otherwise it falls back to the latest OSS thumbnail via ``async_camera_image``.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TengyingDataUpdateCoordinator,
        api: YingTengApi,
        uuid: str,
        name: str,
    ) -> None:
        """Initialize the camera."""
        super().__init__()
        self.coordinator = coordinator
        self.api = api
        self._uuid = uuid
        self._device_name = name
        self._attr_unique_id = f"tengying_camera_{uuid}"
        self._attr_name = name
        self._attr_icon = "mdi:camera"
        self._attr_is_stream = bool(coordinator.rtsp_host)
        self._attr_motion_detection_enabled = False
        self._last_image: bytes | None = None
        _LOGGER.debug("Camera %s (%s) initialized (rtsp=%s)", name, uuid, coordinator.rtsp_host)

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
    def available(self) -> bool:
        """Return if camera is available."""
        return self.coordinator.last_update_success

    @property
    def stream_source(self) -> str | None:
        """Return the RTSP URL produced by the tutk-bridge addon, if configured."""
        rtsp_host = self.coordinator.rtsp_host
        if not rtsp_host:
            return None
        return f"rtsp://{rtsp_host}/tengying_{self._uuid}"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest still image (thumbnail) from the API."""
        try:
            image = await self.api.get_device_thumbnail(self._uuid)
            if image:
                self._last_image = image
                return image
        except Exception as err:
            _LOGGER.debug("Failed to fetch thumbnail from API: %s", err)
        return self._last_image

    async def async_update(self) -> None:
        """Update camera state from coordinator."""
        await self.coordinator.async_request_refresh()

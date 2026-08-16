"""Coordinator for Tengying Smart Camera integration."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import YingTengApi, YingTengAuthError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class TengyingDataUpdateCoordinator(DataUpdateCoordinator[dict]):
    """Class to manage fetching data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: YingTengApi,
        entry_id: str = "",
        rtsp_host: str = "",
        devices_order: list[str] | None = None,
    ) -> None:
        """Initialize."""
        self.api = api
        self.entry_id = entry_id
        # 持久化设备设置（开关乐观态），重启保留
        self._store = (
            Store(hass, 1, f"tengying_camera.{entry_id}.settings")
            if entry_id
            else None
        )
        self.settings: dict[str, bool] = {}
        # Host:port of the tutk-bridge addon (e.g. "192.168.1.100:8554" or
        # "127.0.0.1:8554" when the addon runs on the same box as HA).
        self.rtsp_host = rtsp_host
        # 设备顺序（与 bridge addon options.json 的 devices 数组一致，
        # 决定 PTZ 控制端口 8561+idx 的映射）
        self.devices_order: list[str] = devices_order or []
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def async_load_settings(self) -> None:
        """Load persistent device settings (switch state) from .storage."""
        if self._store is None:
            return
        try:
            data = await self._store.async_load()
            self.settings = data or {}
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("tengying load settings failed: %s", err)
            self.settings = {}

    async def async_save_settings(self) -> None:
        """Persist device settings to .storage."""
        if self._store is None:
            return
        try:
            await self._store.async_save(self.settings)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("tengying save settings failed: %s", err)

    def ctrl_port_for(self, device_id: str) -> int:
        """返回设备对应的 bridge PTZ 控制端口（8561 + 顺序索引）。"""
        if device_id in self.devices_order:
            return 8561 + self.devices_order.index(device_id)
        # 兜底：已知默认映射
        known = {"YT3486ZCX35W": 8561, "YT3586ZENZ3B": 8562}
        return known.get(device_id, 8561)

    def audio_down_port_for(self, device_id: str) -> int:
        """返回设备对应的音频下行端口（8661 + 顺序索引，bridge v0.5.0 起）。"""
        if device_id in self.devices_order:
            return 8661 + self.devices_order.index(device_id)
        # 兜底：与 ctrl_port 同序（8561→8661, 8562→8662）
        known = {"YT3486ZCX35W": 8661, "YT3586ZENZ3B": 8662}
        return known.get(device_id, 8661)

    async def _async_update_data(self) -> dict:
        """Fetch data from API."""
        try:
            devices = await self.api.get_device_list()
        except YingTengAuthError:
            _LOGGER.warning("Token expired, re-authenticating...")
            try:
                await self.api.ensure_auth()
                devices = await self.api.get_device_list()
            except Exception as err:
                raise UpdateFailed(f"Re-auth failed: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Error fetching device data: {err}") from err

        # Enrich with online status (returns is_online per device)
        try:
            devices = await self.api.check_devices_online(devices)
        except Exception as err:
            _LOGGER.debug("Online status check failed: %s", err)

        # 拉取每台设备最新一条告警消息（供 alarm sensor 展示）
        alarms: dict[str, dict] = {}
        try:
            for dev in devices:
                uuid = dev.get("uuid", "")
                if not uuid:
                    continue
                data = await self.api.fetch_alarm_messages(uuid, limit=1)
                items = data.get("items") or []
                if items:
                    alarms[uuid] = items[0]
        except Exception as err:
            _LOGGER.debug("Alarm fetch failed: %s", err)

        result = {"devices": devices, "device_map": {}, "alarms": alarms}
        for d in devices:
            uuid = d["uuid"]
            result["device_map"][uuid] = d

        return result

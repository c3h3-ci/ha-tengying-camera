"""Switch platform for Tengying Smart Camera integration (设备设置: 红外夜视/移动跟踪).

协议（App 逆向 AVIOCTRLDEFs + universal/instructions）:
  日夜模式  查询 32790 / 设置 32792  payload 12B, mode@offset4 LE (0=AUTO 1=OFF 2=ON)
  双光源    查询 32786 / 设置 32788  payload 同构
  移动跟踪  查询 32800 / 设置 32802  payload 同构 (0=OFF 1=ON)
通过 bridge ctrl 端口 (8561+idx) 下发; 设备查询响应 PPCS 层收不到, 采用乐观状态。
"""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import TengyingDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# IOCTRL 命令码（App AVIOCTRLDEFs）
IO_SET_DAYNIGHT = 32792      # 日夜模式
IO_SET_DOUBLELIGHT = 32788   # 双光源(补光灯)
IO_SET_MOTION_TRACK = 32802  # 移动跟踪

# 设备乐观状态（GET 响应在 PPCS 层不可达；重启后回到默认）
_OPT_STATE: dict[str, dict[str, bool]] = {}


def _payload_12b(value: int) -> str:
    """构造 SMsgAVIoctrlSetDoubleLightReq 12B payload: [channel:LE32=0][value:LE32][reserved:LE32=0].

    例 value=2 -> "000000000200000000000000"（mode=2=ON @ offset 4）
    """
    import struct

    return struct.pack("<III", 0, value & 0xFFFFFFFF, 0).hex()


def _send_ioctl(port: int, io: int, payload_hex: str) -> int:
    """向 bridge ctrl 端口下发 IOCTRL 指令，返回设备返回码。"""
    import json
    import socket

    cmd = json.dumps({"io": io, "payload": payload_hex})
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(cmd.encode())
            resp = sock.recv(256).decode(errors="replace")
        _LOGGER.debug("tengying ioctl io=%d -> %s", io, resp)
        return 0
    except OSError as err:
        _LOGGER.warning("tengying ioctl port %d failed: %s", port, err)
        return -1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch platform."""
    coordinator: TengyingDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]

    entities = []
    for device in coordinator.data.get("devices", []):
        uuid = device["uuid"]
        name = device.get("name", uuid)
        entities.append(TengyingNightVisionSwitch(coordinator, uuid, name))
        entities.append(TengyingMotionTrackSwitch(coordinator, uuid, name))

    async_add_entities(entities)


class _TengyingSettingSwitch(CoordinatorEntity, SwitchEntity):
    """设备设置开关基类（乐观状态 + IOCTRL 下发）。"""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TengyingDataUpdateCoordinator,
        uuid: str,
        name: str,
        io_set: int,
        on_value: int,
        off_value: int,
        suffix: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._uuid = uuid
        self._device_name = name
        self._io_set = io_set
        self._on_value = on_value
        self._off_value = off_value
        self._attr_unique_id = f"tengying_{uuid}_{suffix}"
        self._attr_icon = icon
        self._key = f"{uuid}:{suffix}"
        self._suffix_name = ""

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._device_name,
            manufacturer=MANUFACTURER,
            model="智能摄像头",
        )

    @property
    def name(self) -> str:
        return f"{self._device_name} {self._suffix_name}"

    @property
    def is_on(self) -> bool | None:
        return _OPT_STATE.get(self._key, True)  # 默认 ON（红外开/跟踪开）

    async def async_turn_on(self, **kwargs) -> None:
        port = self.coordinator.ctrl_port_for(self._uuid)
        payload = _payload_12b(self._on_value)
        await self.hass.async_add_executor_job(_send_ioctl, port, self._io_set, payload)
        _OPT_STATE[self._key] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        port = self.coordinator.ctrl_port_for(self._uuid)
        payload = _payload_12b(self._off_value)
        await self.hass.async_add_executor_job(_send_ioctl, port, self._io_set, payload)
        _OPT_STATE[self._key] = False
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "uuid": self._uuid,
            "ioctr": self._io_set,
            "payload_on": _payload_12b(self._on_value),
            "payload_off": _payload_12b(self._off_value),
        }


class TengyingNightVisionSwitch(_TengyingSettingSwitch):
    """红外夜视开关（日夜模式 ON/OFF；AUTO 未映射，默认 OFF 对应强制关/红外开）。"""

    def __init__(self, coordinator, uuid, name):
        super().__init__(coordinator, uuid, name,
                         io_set=IO_SET_DAYNIGHT, on_value=2, off_value=1,
                         suffix="night_vision", icon="mdi:weather-night")
        self._suffix_name = "红外夜视"


class TengyingMotionTrackSwitch(_TengyingSettingSwitch):
    """移动跟踪开关。"""

    def __init__(self, coordinator, uuid, name):
        super().__init__(coordinator, uuid, name,
                         io_set=IO_SET_MOTION_TRACK, on_value=1, off_value=0,
                         suffix="motion_track", icon="mdi:radar")
        self._suffix_name = "移动跟踪"

"""Switch platform for Tengying Smart Camera integration.

两类开关：
1) 设备设置开关（红外夜视 / 移动跟踪）：走 bridge ctrl 端口下发 IOCTRL；
   因 PPCS GET 响应不可达，采用乐观状态 + HA Store 持久化（重启保留上次下发值）。
2) 推送开关：每台设备 × 每种事件类型(motion/body/car/pet/sound/ai_summary)
   一个 switch，云端 v2/cloud/switcher 为 truth source，轮询读真值、HA 拨动写回，
   实现 App↔HA 双向同步（不受 PPCS 限制）。

协议（App 逆向 AVIOCTRLDEFs + universal/instructions）:
  日夜模式  查询 32790 / 设置 32792  payload 12B, mode@offset4 LE (0=AUTO 1=OFF 2=ON)
  双光源    查询 32786 / 设置 32788  payload 同构
  移动跟踪  查询 32800 / 设置 32802  payload 同构 (0=OFF 1=ON)
通过 bridge ctrl 端口 (8561+idx) 下发。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN, MANUFACTURER
from .coordinator import TengyingDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# IOCTRL 命令码（App AVIOCTRLDEFs）
IO_SET_DAYNIGHT = 32792      # 日夜模式
IO_SET_DOUBLELIGHT = 32788   # 双光源(补光灯)
IO_SET_MOTION_TRACK = 32802  # 移动跟踪

# 推送开关兜底事件类型（实际以云端 item 为准）
DEFAULT_PUSH_TAGS = ["motion", "body", "car", "pet", "sound", "ai_summary"]
PUSH_SCAN_INTERVAL = 300  # 云端 truth source，5 分钟轮询一次


# ---------- 设备设置开关（红外夜视 / 移动跟踪） ----------

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


class _TengyingSettingSwitch(CoordinatorEntity, SwitchEntity):
    """设备设置开关基类（乐观状态 + IOCTRL 下发 + Store 持久化）。"""

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
        # 持久化在 coordinator.settings（HA Store），重启保留上次下发值
        return self.coordinator.settings.get(self._key, True)  # 默认 ON（红外开/跟踪开）

    async def async_turn_on(self, **kwargs) -> None:
        port = self.coordinator.ctrl_port_for(self._uuid)
        payload = _payload_12b(self._on_value)
        await self.hass.async_add_executor_job(_send_ioctl, port, self._io_set, payload)
        self.coordinator.settings[self._key] = True
        await self.coordinator.async_save_settings()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        port = self.coordinator.ctrl_port_for(self._uuid)
        payload = _payload_12b(self._off_value)
        await self.hass.async_add_executor_job(_send_ioctl, port, self._io_set, payload)
        self.coordinator.settings[self._key] = False
        await self.coordinator.async_save_settings()
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


# ---------- 推送开关（云端双向同步） ----------

class TengyingPushSwitchCoordinator(DataUpdateCoordinator[dict]):
    """轮询各设备推送开关配置（v2/cloud/switcher）。云端为 truth source。"""

    def __init__(self, hass: HomeAssistant, api, devices: list[dict]) -> None:
        self.api = api
        self.devices = devices
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_push",
            update_interval=timedelta(seconds=PUSH_SCAN_INTERVAL),
        )

    async def _async_update_data(self) -> dict:
        result: dict[str, dict] = {}
        for dev in self.devices:
            uuid = dev.get("uuid")
            if not uuid:
                continue
            try:
                result[uuid] = await self.api.get_push_switches(uuid)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("push switches %s failed: %s", uuid, err)
                # 保留旧值，避免设备离线导致实体全变 unknown
                if self.data and uuid in self.data:
                    result[uuid] = self.data[uuid]
        return result


class TengyingPushSwitch(CoordinatorEntity, SwitchEntity):
    """单个事件类型的推送开关，云端双向同步。"""

    _attr_has_entity_name = True

    def __init__(self, coordinator, device_id, device_name, tag, tag_name) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._tag = tag
        self._device_name = device_name
        self._attr_unique_id = f"tengying_{device_id}_push_{tag}"
        self._attr_name = f"推送 {tag_name}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._device_name,
            manufacturer=MANUFACTURER,
            model="智能摄像头",
        )

    @property
    def is_on(self) -> bool | None:
        push = self.coordinator.data.get(self._device_id) if self.coordinator.data else None
        if not push:
            return None
        for it in push.get("item") or []:
            if it.get("tag") == self._tag:
                return bool(it.get("status"))
        return None

    async def _set(self, enabled: bool) -> None:
        """读当前完整配置 → 改对应 tag → 整包写回云端 → 刷新。"""
        api = self.coordinator.api
        try:
            current = await api.get_push_switches(self._device_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("push set get failed %s: %s", self._device_id, err)
            return
        item = current.get("item") or []
        found = False
        for it in item:
            if it.get("tag") == self._tag:
                it["status"] = enabled
                found = True
        if not found:
            item.append({"tag": self._tag, "name": self._tag, "status": enabled})
            current["item"] = item
        try:
            await api.update_push_switches(self._device_id, current)
            await self.coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("push set update failed %s: %s", self._device_id, err)

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)


# ---------- 平台入口 ----------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch platform (设备设置开关 + 推送开关)."""
    coordinator: TengyingDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    devices = []
    if coordinator.data:
        devices = coordinator.data.get("devices", [])

    entities = []
    for device in devices:
        uuid = device["uuid"]
        name = device.get("name", uuid)
        # 设备设置开关
        entities.append(TengyingNightVisionSwitch(coordinator, uuid, name))
        entities.append(TengyingMotionTrackSwitch(coordinator, uuid, name))

    # 推送开关（独立 Coordinator，云端双向同步）
    if devices:
        push_coordinator = TengyingPushSwitchCoordinator(hass, coordinator.api, devices)
        # 关键：绝不在 setup 关键路径无限阻塞。
        # get_push_switches 云调用偶发卡顿/挂起，会让整个 entry 的 setup 超过
        # HA 超时上限而被回滚（SENSOR/CAMERA/SWITCH 实体全部丢失，仅遗留旧的
        # bridge 孤儿实体）。因此首次刷新限时 12s；失败/超时则先用兜底 tags
        # 建实体，后台继续刷新补足云端真值。
        try:
            await asyncio.wait_for(
                push_coordinator.async_config_entry_first_refresh(), timeout=12
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("tengying push first refresh failed/slow: %s", err)
            hass.async_create_task(push_coordinator.async_request_refresh())
        for dev in devices:
            uuid = dev.get("uuid")
            name = dev.get("name", uuid)
            if not uuid:
                continue
            push = push_coordinator.data.get(uuid) if push_coordinator.data else None
            tags = [
                (it.get("tag"), it.get("name") or it.get("tag"))
                for it in (push.get("item") or [])
                if it.get("tag")
            ]
            if not tags:
                tags = [(t, t) for t in DEFAULT_PUSH_TAGS]
            for tag, tag_name in tags:
                entities.append(
                    TengyingPushSwitch(push_coordinator, uuid, name, tag, tag_name)
                )

    async_add_entities(entities)

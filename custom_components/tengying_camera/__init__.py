"""Init for Tengying Smart Camera integration."""
from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import YingTengApi
from .cloud_download import CloudRecordDownloader
from .const import DOMAIN
from .coordinator import TengyingDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.CAMERA, Platform.SWITCH]

# PTZ 方向 -> 设备协议方向常量（AVIOCTRLDEFs）
PTZ_DIRECTIONS = {
    "stop": 0,
    "up": 1,
    "down": 2,
    "left": 3,
    "left_up": 4,
    "left_down": 5,
    "right": 6,
    "right_up": 7,
    "right_down": 8,
}


def _send_ctrl_cmd(port: int, io: int, payload_hex: str) -> int:
    """向 bridge 控制端口发送 IOCTRL 命令，返回设备返回码。"""
    cmd = f'{{"io":{io},"payload":"{payload_hex}"}}'
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(cmd.encode())
            resp = sock.recv(128).decode(errors="replace")
        _LOGGER.debug("tengying ctrl %s -> %s", cmd, resp)
        return 0
    except OSError as err:
        _LOGGER.warning("tengying ctrl port %d failed: %s", port, err)
        return -1


async def async_ptz_service(hass: HomeAssistant, coordinator, call: ServiceCall) -> None:
    """处理云台控制服务调用。"""
    device_id = str(call.data.get("device_id", ""))
    direction = str(call.data.get("direction", "stop"))
    speed = int(call.data.get("speed", 100))
    duration_ms = int(call.data.get("duration", 500))
    if speed < 1:
        speed = 1
    if speed > 255:
        speed = 255

    d = PTZ_DIRECTIONS.get(direction, 0)
    port = coordinator.ctrl_port_for(device_id)
    payload = bytes([d, 0, 0, 0, 0, speed]).hex()

    _LOGGER.info("tengying PTZ: dev=%s dir=%s speed=%d port=%d", device_id, direction, speed, port)
    await hass.async_add_executor_job(_send_ctrl_cmd, port, 4097, payload)

    # 自动停止（防止云台一直转）
    if duration_ms > 0 and d != 0:
        await asyncio.sleep(duration_ms / 1000)
        await hass.async_add_executor_job(
            _send_ctrl_cmd, port, 4097, bytes([0, 0, 0, 0, 0, speed]).hex()
        )


def _setup_ptz_service(hass: HomeAssistant, coordinator) -> None:
    """注册 ptz 服务（重复注册自动覆盖）。"""
    async def handler(call: ServiceCall) -> None:
        await async_ptz_service(hass, coordinator, call)

    hass.services.async_register(DOMAIN, "ptz", handler, supports_response=None)


async def async_list_records_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """查询云端录像列表（v2/cloud/videos/{dev}/{date}）。

    返回 {"des_key":..., "items":[{start_time,end_time,ossid}]}，
    供前端/自动化展示录像时段。
    """
    device_id = str(call.data.get("device_id", ""))
    date = str(call.data.get("date", ""))
    api = coordinator.api
    try:
        data = await api.fetch_cloud_records(device_id, date)
        _LOGGER.info(
            "tengying records: dev=%s date=%s total=%s",
            device_id, date, len(data.get("items", [])),
        )
        return data
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("tengying records failed: %s", err)
        return {"error": str(err)}


def _setup_record_service(hass: HomeAssistant, coordinator) -> None:
    """注册 list_records 服务。"""
    async def handler(call: ServiceCall) -> dict:
        return await async_list_records_service(hass, coordinator, call)

    hass.services.async_register(
        DOMAIN, "list_records", handler, supports_response=SupportsResponse.ONLY
    )


async def async_download_record_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """下载指定时间段云端录像 → H265 裸流（含 G711A 音频），存到 /config/www/tengying_records/。

    参数:
      device_id: 设备 UUID
      date:      YYYY-MM-DD（默认今天）
      start_time: 起始 Unix 秒（默认录像段起点）
      end_time:   结束 Unix 秒（默认录像段终点）
    返回:
      {"status", "video"(HA 路径), "local_url"(/local/...), "video_frames",
       "video_bytes", "audio_bytes", "downloaded", "missing"}
    """
    device_id = str(call.data.get("device_id", ""))
    date = str(call.data.get("date", "") or datetime.now().strftime("%Y-%m-%d"))
    api = coordinator.api

    try:
        records = await api.fetch_cloud_records(device_id, date)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("tengying download: list failed: %s", err)
        return {"status": "error", "error": f"list_records failed: {err}"}

    des_key = records.get("des_key", "")
    items = records.get("items") or []
    if not items:
        return {"status": "empty", "error": f"{date} 无云端录像"}

    # 默认整个录像段范围（第一段起点 ~ 最后一段终点）
    start_s = int(call.data.get("start_time", 0) or 0)
    end_s = int(call.data.get("end_time", 0) or 0)
    if not start_s:
        start_s = min(it["start_time"] for it in items)
    if not end_s:
        end_s = max(it["end_time"] for it in items)

    # 只下载与 [start_s, end_s) 交叠的段
    segments = [
        it for it in items
        if it["end_time"] > start_s and it["start_time"] < end_s
    ]
    if not segments:
        return {"status": "empty", "error": "时间段内无录像段"}

    www_dir = Path(hass.config.path("www")) / "tengying_records"
    downloader = CloudRecordDownloader(api)
    base_name = f"{device_id}_{start_s}_{end_s}"
    total = {"video_frames": 0, "video_bytes": 0, "audio_bytes": 0,
             "downloaded": 0, "missing": 0}

    # 按段下载（每段独立文件，避免跨段 ossid 混用 token）
    def _to_ms(v: int) -> int:
        # fetch_cloud_records 返回秒级时间戳；个别设备可能返回毫秒，统一归一化为毫秒
        return v * 1000 if v < 1_000_000_000_000 else v

    for idx, seg in enumerate(segments):
        seg_start = _to_ms(max(start_s, seg["start_time"]))
        seg_end = _to_ms(min(end_s, seg["end_time"]))
        if seg_end <= seg_start:
            continue
        ossid = seg.get("ossid") or seg.get("oss_id") or ""
        result = await downloader.download_segment(
            device_id, ossid, des_key, seg_start, seg_end,
            www_dir, f"{base_name}_seg{idx}",
        )
        for k in ("video_frames", "video_bytes", "audio_bytes",
                  "downloaded", "missing"):
            total[k] += result.get(k, 0)

    # 合并分段文件（H265 裸流/G711A 可直接级联）
    seg_files = sorted(www_dir.glob(f"{base_name}_seg*.h265"))
    if not seg_files:
        return {"status": "error", "error": "下载失败：无视频数据"}
    merged = www_dir / f"{base_name}.h265"
    with open(merged, "wb") as out:
        for p in seg_files:
            out.write(p.read_bytes())
            p.unlink(missing_ok=True)
    # 合并音频段（G711A A-law，320B/帧）
    audio_files = sorted(www_dir.glob(f"{base_name}_seg*.g711a"))
    merged_audio = www_dir / f"{base_name}.g711a"
    with open(merged_audio, "wb") as out:
        for p in audio_files:
            out.write(p.read_bytes())
            p.unlink(missing_ok=True)

    # 可选: ffmpeg 转 mp4（H265 copy + G711A→AAC；HA 容器自带 ffmpeg）
    # 注意: 不能加 -shortest（视频流时间戳未设置会切光音频）
    mp4_url = None
    mp4_path = None
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        mp4_path = www_dir / f"{base_name}.mp4"
        try:
            duration = str(max(1, end_s - start_s))
            cmd = [
                ffmpeg, "-y",
                "-f", "hevc", "-framerate", "15", "-i", str(merged),
                "-f", "alaw", "-ar", "8000", "-ac", "1", "-i", str(merged_audio),
                "-c:v", "copy", "-c:a", "aac", "-t", duration,
                str(mp4_path),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=300)
            if mp4_path.exists() and mp4_path.stat().st_size > 0:
                mp4_url = f"/local/tengying_records/{mp4_path.name}"
                _LOGGER.info("tengying mp4 ok: %s", mp4_path)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("tengying mp4 convert failed: %s", err)
            mp4_path = None

    local_url = f"/local/tengying_records/{merged.name}"
    _LOGGER.info("tengying download ok: %s (%d frames, %d B)",
                 merged, total["video_frames"], total["video_bytes"])
    return {
        "status": "ok",
        "video": str(merged),
        "local_url": local_url,
        "mp4": str(mp4_path) if mp4_path else None,
        "mp4_url": mp4_url,
        "start_time": start_s,
        "end_time": end_s,
        **total,
    }


def _setup_download_service(hass: HomeAssistant, coordinator) -> None:
    """注册 download_record 服务。"""
    async def handler(call: ServiceCall) -> dict:
        return await async_download_record_service(hass, coordinator, call)

    hass.services.async_register(
        DOMAIN, "download_record", handler, supports_response=SupportsResponse.ONLY
    )


async def async_list_messages_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """查询告警消息列表（POST v2/cloud/event）。

    参数: device_id / date(默认今天) / tag(可选: motion/body/car/pet/sound/ai_summary)
          / offset / limit
    返回: {"items": [{id, thumbnail, image, time, can_play, tag}], "total": n}
    """
    device_id = str(call.data.get("device_id", ""))
    date = str(call.data.get("date", "") or datetime.now().strftime("%Y-%m-%d"))
    tag = call.data.get("tag")
    tag = str(tag) if tag else None
    offset = int(call.data.get("offset", 0) or 0)
    limit = int(call.data.get("limit", 30) or 30)
    try:
        data = await coordinator.api.fetch_alarm_messages(
            device_id, date, tag=tag, offset=offset, limit=limit
        )
        items = data.get("items") or []
        _LOGGER.info("tengying messages: dev=%s date=%s tag=%s total=%d",
                     device_id, date, tag, len(items))
        return {"status": "ok", "items": items, "total": len(items)}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("tengying messages failed: %s", err)
        return {"status": "error", "error": str(err)}


async def async_list_categories_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """查询告警消息分类（POST v2/cloud/filter）。"""
    device_id = str(call.data.get("device_id", ""))
    try:
        categories = await coordinator.api.fetch_message_categories(device_id)
        return {"status": "ok", "categories": categories}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("tengying categories failed: %s", err)
        return {"status": "error", "error": str(err)}


async def async_get_push_switch_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """查询推送开关配置（GET v2/cloud/switcher/{dev}）。"""
    device_id = str(call.data.get("device_id", ""))
    try:
        data = await coordinator.api.get_push_switches(device_id)
        return {"status": "ok", **data}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("tengying push switches failed: %s", err)
        return {"status": "error", "error": str(err)}


async def async_set_push_switch_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """更新推送开关（POST v2/cloud/switcher/{dev}）。

    参数: device_id / tag(事件类型) / enabled(bool) / global_enabled(bool, 可选)
    说明: 单事件开关基于当前配置做局部修改后整包提交（App 同款行为）
    """
    device_id = str(call.data.get("device_id", ""))
    tag = call.data.get("tag")
    api = coordinator.api
    try:
        current = await api.get_push_switches(device_id)
        item = current.get("item") or []
        if tag:
            enabled = bool(call.data.get("enabled", True))
            updated = False
            for it in item:
                if it.get("tag") == tag:
                    it["status"] = enabled
                    updated = True
            if not updated:
                item.append({"tag": str(tag), "name": str(tag), "status": enabled})
        if call.data.get("global_enabled") is not None:
            current["global_status"] = bool(call.data["global_enabled"])
        await api.update_push_switches(device_id, current)
        _LOGGER.info("tengying push switch updated: dev=%s tag=%s", device_id, tag)
        return {"status": "ok"}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("tengying set push switch failed: %s", err)
        return {"status": "error", "error": str(err)}


def _setup_message_services(hass: HomeAssistant, coordinator) -> None:
    """注册告警消息相关服务（list_messages / list_message_categories / get_push_switches / set_push_switches）。"""
    for name, handler in (
        ("list_messages", async_list_messages_service),
        ("list_message_categories", async_list_categories_service),
        ("get_push_switches", async_get_push_switch_service),
        ("set_push_switch", async_set_push_switch_service),
    ):
        async def make(call: ServiceCall, _h=handler):
            return await _h(hass, coordinator, call)

        hass.services.async_register(
            DOMAIN, name, make, supports_response=SupportsResponse.ONLY
        )


async def async_device_command_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """下发任意 IOCTRL 设备指令（走 bridge 控制通道）。

    参数: device_id / io(指令码) / payload(hex 字符串)
    常用指令码:
      32792 日夜模式  payload 12B mode@4 (0=AUTO 1=OFF 2=ON)
      32788 双光源    payload 同构
      32802 移动跟踪  payload 同构 (0=OFF 1=ON)
      32784 设备重启
    """
    device_id = str(call.data.get("device_id", ""))
    io = int(call.data.get("io", 0) or 0)
    payload = str(call.data.get("payload", "") or "")
    port = coordinator.ctrl_port_for(device_id)
    try:
        await hass.async_add_executor_job(_send_ctrl_cmd, port, io, payload)
        _LOGGER.info("tengying device cmd: dev=%s io=%d payload=%s", device_id, io, payload)
        return {"status": "ok"}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("tengying device cmd failed: %s", err)
        return {"status": "error", "error": str(err)}


def _build_play_record_payload(start_time: int, command: int) -> str:
    """构造 SD 卡回放控制 payload（SMsgAVIoctrlPlayRecord 24B）。

    [Param:LE32=0][avIndex:LE32=0][channel:LE32=0][stTimeDay:8B][command:LE32]
    stTimeDay: [year:LE16][month][day][wday][hour][minute][second]
    command: 16=START 1=STOP 0=PAUSE 7=END 8=CONTINUE
    """
    import struct

    t = datetime.fromtimestamp(start_time)
    wday = t.isoweekday()  # 1-7
    st = struct.pack("<H", t.year) + bytes(
        [t.month, t.day, wday, t.hour, t.minute, t.second]
    )
    return (struct.pack("<III", 0, 0, 0) + st + struct.pack("<I", command)).hex()


def _sframe_header(flags: int, cam_index: int, frame_size: int, ts: int) -> bytes:
    """SFrameInfo 16B 头: [codec_id:LE16=138][flags][cam_index][0][reserved3][frame_size:LE32][ts:LE32]."""
    import struct

    return (
        struct.pack("<H", 138)
        + bytes([flags, cam_index, 0, 0, 0, 0])
        + struct.pack("<I", frame_size)
        + struct.pack("<I", ts)
    )


def _send_audio_chunk(port: int, frame_hex: str) -> int:
    """向 bridge 发送单个音频帧（SFrameInfo+G711A），返回 0=成功。"""
    import json
    import socket

    cmd = json.dumps({"audio": frame_hex})
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=6) as sock:
            sock.sendall(cmd.encode())
            sock.recv(256)
        return 0
    except OSError as err:
        _LOGGER.warning("tengying audio port %d failed: %s", port, err)
        return -1


async def async_talkback_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """语音对讲（单向喊话）：音频文件 → 8kHz A-law → PPCS_Write(ch5) 上行。

    参数: device_id / file(音频文件绝对路径) / auto_start(默认 true: 先 848 启动扬声器)
    """
    import shutil
    import struct
    import subprocess
    import tempfile

    device_id = str(call.data.get("device_id", ""))
    audio_file = str(call.data.get("file", "") or "")
    auto_start = bool(call.data.get("auto_start", True))
    port = coordinator.ctrl_port_for(device_id)

    if not audio_file or not Path(audio_file).exists():
        return {"status": "error", "error": f"audio file not found: {audio_file}"}
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"status": "error", "error": "ffmpeg not available"}

    if auto_start:
        await hass.async_add_executor_job(
            _send_ctrl_cmd, port, 848, "0100000000000000"
        )
        await asyncio.sleep(0.5)

    # 1. ffmpeg 转 8kHz 16bit mono PCM（容器 ffmpeg 无 alaw 编码器，PCM 一定支持）
    with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as tmp:
        pcm_path = tmp.name
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", audio_file,
            "-ar", "8000", "-ac", "1", "-f", "s16le", pcm_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=60)
        pcm = Path(pcm_path).read_bytes()
    finally:
        Path(pcm_path).unlink(missing_ok=True)
    if len(pcm) < 2:
        return {"status": "error", "error": "audio convert failed"}

    # 2. PCM16 → 标准 A-law（与 App G711Code.linear2alaw 一致）
    def linear2alaw(s: int) -> int:
        if s >= 0:
            mask = 0xD5
        else:
            s = (-s) - 1
            mask = 0x55
        if s >= 256:
            seg = 0
            while s > 0x3FF:
                seg += 1
                s >>= 1
            alaw = (seg << 4) | ((s >> 1) & 0x0F)
        else:
            alaw = (s >> 4) & 0x0F
            if alaw > 7:
                alaw = 7
            alaw = (alaw << 4) | 0x08
        return (alaw ^ mask) & 0xFF

    samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
    data = bytes(linear2alaw(s) for s in samples)

    # 3. 分块上行（每块 1600B + 16B SFrameInfo 头）
    chunk_count = 0
    ts = 0
    for i in range(0, len(data), 1600):
        chunk = data[i : i + 1600]
        frame = _sframe_header(2, 5, len(chunk), ts) + chunk
        ts += len(chunk) * 1000 // 8000
        await hass.async_add_executor_job(_send_audio_chunk, port, frame.hex())
        chunk_count += 1
        await asyncio.sleep(0.03)

    _LOGGER.info("tengying talkback: dev=%s file=%s chunks=%d", device_id, audio_file, chunk_count)
    return {"status": "ok", "chunks": chunk_count, "bytes": len(data)}


def _setup_talkback_service(hass: HomeAssistant, coordinator) -> None:
    """注册 talkback 服务。"""
    async def handler(call: ServiceCall) -> dict:
        return await async_talkback_service(hass, coordinator, call)

    hass.services.async_register(
        DOMAIN, "talkback", handler, supports_response=SupportsResponse.ONLY
    )


def _recv_audio_frame(sock: socket.socket, timeout: float = 3.0) -> bytes | None:
    """从音频下行端口读一帧 [16B头 + payload]，返回完整帧；超时/断开返回 None。"""
    sock.settimeout(timeout)
    head = b""
    while len(head) < 4:
        chunk = sock.recv(4 - len(head))
        if not chunk:
            return None
        head += chunk
    import struct

    flen = struct.unpack("<I", head)[0]
    if flen < 16 or flen > 8192:
        return None
    body = b""
    while len(body) < flen:
        chunk = sock.recv(flen - len(body))
        if not chunk:
            return None
        body += chunk
    return body


async def async_audio_down_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """双向语音·下行（听设备麦克风）：连接音频下行端口采集 N 秒 → 存 g711a + 转 wav。

    参数: device_id / duration(秒, 默认 10)
    返回: {"status", "g711a"(HA 路径), "wav"(HA 路径), "wav_url"(/local/...),
           "frames", "bytes", "port"}
    """
    import struct

    device_id = str(call.data.get("device_id", ""))
    duration = float(call.data.get("duration", 10) or 10)
    if duration < 1:
        duration = 1
    if duration > 120:
        duration = 120
    port = coordinator.audio_down_port_for(device_id)
    www_dir = Path(hass.config.path("www")) / "tengying_records"
    www_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"{device_id}_down_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    frames = 0
    audio_bytes = 0
    g711a_path = www_dir / f"{base_name}.g711a"
    deadline = time.monotonic() + duration

    def _collect() -> tuple[int, int]:
        n = 0
        size = 0
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            with open(g711a_path, "wb") as fout:
                while time.monotonic() < deadline:
                    frame = _recv_audio_frame(sock, timeout=1.0)
                    if frame is None:
                        break
                    payload = frame[16:]
                    if payload:
                        fout.write(payload)
                        n += 1
                        size += len(payload)
        return n, size

    try:
        frames, audio_bytes = await hass.async_add_executor_job(_collect)
    except OSError as err:
        _LOGGER.warning("tengying audio_down port %d failed: %s", port, err)
        return {"status": "error", "error": str(err), "port": port}
    if not frames:
        return {"status": "error", "error": "无音频数据（设备未推流）", "port": port}

    # 转 wav（HA 容器 ffmpeg 支持 alaw 解码）
    wav_path = www_dir / f"{base_name}.wav"
    wav_url = None
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, "-y", "-f", "alaw", "-ar", "8000", "-ac", "1",
                "-i", str(g711a_path), "-ar", "8000", "-ac", "1",
                str(wav_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=60)
            if wav_path.exists() and wav_path.stat().st_size > 0:
                wav_url = f"/local/tengying_records/{wav_path.name}"
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("tengying audio_down wav convert failed: %s", err)

    _LOGGER.info("tengying audio_down: dev=%s frames=%d bytes=%d port=%d",
                 device_id, frames, audio_bytes, port)
    return {
        "status": "ok",
        "g711a": str(g711a_path),
        "wav": str(wav_path) if wav_path.exists() else None,
        "wav_url": wav_url,
        "frames": frames,
        "bytes": audio_bytes,
        "duration": duration,
        "port": port,
    }


def _setup_audio_down_service(hass: HomeAssistant, coordinator) -> None:
    """注册 audio_down 服务。"""
    async def handler(call: ServiceCall) -> dict:
        return await async_audio_down_service(hass, coordinator, call)

    hass.services.async_register(
        DOMAIN, "audio_down", handler, supports_response=SupportsResponse.ONLY
    )


async def async_record_play_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """SD 卡录像回放控制（IOCTRL 794，走 bridge 控制通道）。

    参数: device_id / start_time(Unix秒, 默认现在) / duration(秒, 默认60, 0=不自动停)
          / command(start|stop|pause|continue|end, 默认 start)
    说明: 回放期间设备把视频流切到 SD 卡录像内容（camera 实体同一 RTSP 路径），
          stop 后 run.sh 自动恢复直播。
    """
    device_id = str(call.data.get("device_id", ""))
    command = str(call.data.get("command", "start"))
    cmd_map = {"start": 16, "stop": 1, "pause": 0, "continue": 8, "end": 7}
    cmd_val = cmd_map.get(command, 16)
    start_time = int(call.data.get("start_time", 0) or 0) or int(datetime.now().timestamp())
    duration = int(call.data.get("duration", 60) or 0)
    port = coordinator.ctrl_port_for(device_id)

    if cmd_val == 16:
        payload = _build_play_record_payload(start_time, 16)
        await hass.async_add_executor_job(_send_ctrl_cmd, port, 794, payload)
        _LOGGER.info("tengying record_play START dev=%s t=%d dur=%d", device_id, start_time, duration)
        if duration > 0:
            await asyncio.sleep(duration)
            stop_payload = _build_play_record_payload(start_time, 1)
            await hass.async_add_executor_job(_send_ctrl_cmd, port, 794, stop_payload)
            _LOGGER.info("tengying record_play STOP dev=%s", device_id)
    else:
        payload = _build_play_record_payload(start_time, cmd_val)
        await hass.async_add_executor_job(_send_ctrl_cmd, port, 794, payload)
        _LOGGER.info("tengying record_play %s dev=%s", command, device_id)
    return {"status": "ok", "command": command, "start_time": start_time}


def _setup_record_play_service(hass: HomeAssistant, coordinator) -> None:
    """注册 record_play 服务。"""
    async def handler(call: ServiceCall) -> dict:
        return await async_record_play_service(hass, coordinator, call)

    hass.services.async_register(
        DOMAIN, "record_play", handler, supports_response=SupportsResponse.ONLY
    )


async def async_share_code_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """生成设备共享码（GET /v2/share/code/{dev}/{timeout}）。"""
    device_id = str(call.data.get("device_id", ""))
    timeout_s = int(call.data.get("timeout", 600) or 600)
    try:
        data = await coordinator.api.share_generate_code(device_id, timeout_s)
        _LOGGER.info("tengying share code: dev=%s code=%s", device_id, data.get("code"))
        return {"status": "ok", "code": data.get("code", "")}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("tengying share code failed: %s", err)
        return {"status": "error", "error": str(err)}


async def async_share_list_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """查询设备共享列表。"""
    device_id = str(call.data.get("device_id", ""))
    try:
        data = await coordinator.api.share_list(device_id)
        return {"status": "ok", **data}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("tengying share list failed: %s", err)
        return {"status": "error", "error": str(err)}


async def async_share_cancel_service(
    hass: HomeAssistant, coordinator, call: ServiceCall
) -> dict:
    """取消设备共享。"""
    device_id = str(call.data.get("device_id", ""))
    share_id = int(call.data.get("share_id", 0) or 0)
    share_type = int(call.data.get("share_type", 1) or 1)
    try:
        await coordinator.api.share_cancel(device_id, share_id, share_type)
        return {"status": "ok"}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("tengying share cancel failed: %s", err)
        return {"status": "error", "error": str(err)}


def _setup_share_services(hass: HomeAssistant, coordinator) -> None:
    """注册设备共享服务（share_code / share_list / share_cancel）。"""
    for name, handler in (
        ("share_code", async_share_code_service),
        ("share_list", async_share_list_service),
        ("share_cancel", async_share_cancel_service),
    ):
        async def make(call: ServiceCall, _h=handler):
            return await _h(hass, coordinator, call)

        hass.services.async_register(
            DOMAIN, name, make, supports_response=SupportsResponse.ONLY
        )


def _setup_device_command_service(hass: HomeAssistant, coordinator) -> None:
    """注册 device_command 服务。"""
    async def handler(call: ServiceCall) -> dict:
        return await async_device_command_service(hass, coordinator, call)

    hass.services.async_register(
        DOMAIN, "device_command", handler, supports_response=SupportsResponse.ONLY
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tengying from a config entry."""
    _LOGGER.warning("tengying DEBUG: async_setup_entry called, entry=%s", entry.entry_id)
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    session = async_get_clientsession(hass)
    api = YingTengApi(session, username=username, password=password)

    # Login
    await api.login(username, password)
    _LOGGER.warning("tengying DEBUG: login ok")

    # Coordinator
    rtsp_host = entry.data.get("rtsp_host") or entry.options.get("rtsp_host", "")
    devices_order = entry.data.get("devices") or entry.options.get("devices")
    coordinator = TengyingDataUpdateCoordinator(
        hass, api, rtsp_host=rtsp_host, devices_order=devices_order
    )
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.warning("tengying DEBUG: coordinator refresh ok")

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    # 注册 PTZ 云台控制 + 录像列表 + 录像下载 + 告警消息 + 设备指令 + SD 回放 + 共享服务
    _setup_ptz_service(hass, coordinator)
    _setup_record_service(hass, coordinator)
    _setup_download_service(hass, coordinator)
    _setup_message_services(hass, coordinator)
    _setup_device_command_service(hass, coordinator)
    _setup_record_play_service(hass, coordinator)
    _setup_share_services(hass, coordinator)
    _setup_talkback_service(hass, coordinator)
    _setup_audio_down_service(hass, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

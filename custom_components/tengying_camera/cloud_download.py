"""腾影智联云端录像下载模块（APK 逆向协议，2026-08-09 实测打通）。

链路:
  1. GET /v2/cloud/videos/{dev}/{date}            -> des_key + items[{start_time,end_time,ossid}]
  2. GET /v2/cloud/oss-token-by-user/{dev}?oss_id= -> StorageAccessToken
  3. .data 路径: {root_path}/{yyyy/MM/dd/HH/mm-ss}.data  (5秒取整, 设备时区)
  4. OSS 签名 URL: StringToSign = "GET\\n\\n\\n{exp}\\n/{bucket}/{key}?security-token={st}"
     (security-token 参与签名, 阿里 SDK SIGNED_PARAMTERS)
  5. 帧头 16B(BE): [0][1=media(1=H264,13=H265,2=G711A,0=TS)][2:4=keyFrame]
     [4:8=length][8:12=timestamp][12:16=encodeType] + payload
  6. encodeType==1 -> payload 需 DES/CBC/PKCS5Padding 解密
     密钥=des_key UTF-8 字节, IV={1,2,3,4,5,6,7,8}
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

DES_IV = bytes([1, 2, 3, 4, 5, 6, 7, 8])

# 设备默认时区（App requireTimezone 接口未接入时用 +08:00）
DEFAULT_TZ = timezone(timedelta(hours=8))


def ts5(ms: int) -> int:
    """DateUtil.getTimestampFiveSec: 向下取整到 5 秒（毫秒）。"""
    s = ms // 1000
    return (s - (s % 5)) * 1000


def des_decrypt(data: bytes, key: str) -> bytes:
    """DES/CBC/PKCS5Padding。key 为 des_key 的 UTF-8 字节（8 字符）。"""
    from Crypto.Cipher import DES  # noqa: PLC0415

    cipher = DES.new(key.encode("utf-8")[:8], DES.MODE_CBC, DES_IV)
    return cipher.decrypt(data)


def frame_path(root_path: str, ts_ms: int, tz: timezone = DEFAULT_TZ) -> str:
    """构造 .data 对象键（5 秒取整 + 设备时区）。"""
    return f"{root_path}/{datetime.fromtimestamp(ts5(ts_ms) / 1000, tz).strftime('%Y/%m/%d/%H/%M-%S')}.data"


def sign_oss_url(token: dict[str, Any], obj_key: str) -> str:
    """构造阿里云 OSS 签名 GET URL（等价 App ObjectURLPresigner + STS）。

    token 字段: access_key_id / access_key_secret / security_token /
                expiration_int / bucket / end_point
    """
    ak = token["access_key_id"]
    sk = token["access_key_secret"]
    st = token["security_token"]
    exp = int(token["expiration_int"])
    bucket = token["bucket"]
    # security-token 在阿里 SDK 的 SIGNED_PARAMTERS 中，必须参与签名
    content = f"GET\n\n\n{exp}\n/{bucket}/{obj_key}?security-token={st}"
    sig = base64.b64encode(
        hmac.new(sk.encode(), content.encode(), hashlib.sha1).digest()
    ).decode()
    return (
        f"{token['end_point'].rstrip('/')}/{obj_key}"
        f"?Expires={exp}"
        f"&OSSAccessKeyId={quote(ak, safe='')}"
        f"&Signature={quote(sig, safe='')}"
        f"&security-token={quote(st, safe='')}"
    )


def parse_data_file(
    raw: bytes, des_key: str, video_out, audio_out, stats: dict
) -> None:
    """解析单个 .data 文件（多帧），提取 H264/H265 到 video_out、G711A 到 audio_out。"""
    off = 0
    while off + 16 <= len(raw):
        head = raw[off : off + 16]
        media_type = head[1]
        length = struct.unpack(">I", head[4:8])[0]
        encode_type = struct.unpack(">I", head[12:16])[0]
        payload = raw[off + 16 : off + 16 + length]
        if len(payload) < length:
            break
        if encode_type == 1 and payload:  # 实测: enc=1 -> 需要 DES 解密
            try:
                payload = des_decrypt(payload, des_key)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("tengying DES fail: %s", err)
                off += 16 + length
                continue
        if media_type in (1, 13) and payload:
            video_out.write(payload)
            stats["video_bytes"] += len(payload)
            stats["video_frames"] += 1
        elif media_type == 2 and payload:
            audio_out.write(payload)
            stats["audio_bytes"] += len(payload)
        off += 16 + length


class CloudRecordDownloader:
    """云端录像段下载器：5 秒粒度循环拉取 .data 并解密提取。"""

    def __init__(self, api) -> None:
        self.api = api
        self._token_cache: dict[str, dict[str, Any]] = {}

    async def _token_for(self, device_id: str, ossid: str) -> dict[str, Any]:
        if ossid not in self._token_cache:
            self._token_cache[ossid] = await self.api.get_cloud_oss_token(
                device_id, ossid
            )
        return self._token_cache[ossid]

    async def _download_data(self, device_id: str, ossid: str, ts_ms: int) -> bytes:
        """下载单个 .data 文件；404/网络失败返回 b''。"""
        token = await self._token_for(device_id, ossid)
        obj_key = frame_path(token["root_path"], ts_ms)
        url = sign_oss_url(token, obj_key)
        async with self.api._session.get(  # noqa: SLF001
            url, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            if resp.status != 200:
                return b""
            return await resp.read()

    async def download_segment(
        self,
        device_id: str,
        ossid: str,
        des_key: str,
        start_ms: int,
        end_ms: int,
        out_dir: Path,
        base_name: str = "record",
    ) -> dict[str, Any]:
        """下载 [start_ms, end_ms) 时间段录像（5 秒粒度）。

        返回 {"video": Path(h265), "audio": Path(g711a), "files": n,
              "missing": n, "video_frames": n, "video_bytes": n, "audio_bytes": n}
        """
        import aiohttp  # noqa: PLC0415  (kept for clarity; module-level import above)

        out_dir.mkdir(parents=True, exist_ok=True)
        vpath = out_dir / f"{base_name}.h265"
        apath = out_dir / f"{base_name}.g711a"
        stats = {"video_bytes": 0, "video_frames": 0, "audio_bytes": 0,
                 "downloaded": 0, "missing": 0}
        t = start_ms
        with open(vpath, "wb") as fv, open(apath, "wb") as fa:
            while t < end_ms:
                raw = await self._download_data(device_id, ossid, t)
                if raw:
                    parse_data_file(raw, des_key, fv, fa, stats)
                    stats["downloaded"] += 1
                else:
                    stats["missing"] += 1
                t += 5000
        _LOGGER.info(
            "tengying cloud dl: dev=%s files=%d missing=%d video=%dB/%df audio=%dB",
            device_id, stats["downloaded"], stats["missing"],
            stats["video_bytes"], stats["video_frames"], stats["audio_bytes"],
        )
        return {
            "video": str(vpath), "audio": str(apath), **stats,
        }

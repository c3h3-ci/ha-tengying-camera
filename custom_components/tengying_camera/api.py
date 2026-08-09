"""Tengying Smart Camera API client."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

from .const import (
    APP_ID,
    APP_PKGNAME,
    APP_SDK_VERSION,
    APP_VERSION,
    DEFAULT_HEADERS,
    EP_SERVICE_URL,
)

_LOGGER = logging.getLogger(__name__)


class YingTengAuthError(Exception):
    """Authentication error."""


class YingTengApiError(Exception):
    """API error."""


class YingTengApi:
    """Tengying Smart Camera API client."""

    def __init__(self, session: aiohttp.ClientSession, username: str = "", password: str = "") -> None:
        self._session = session
        self._username = username
        self._password = password
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._user_id: int | None = None
        self._api_base: str | None = None  # api-cn01.tange365.com
        self._open_api_base: str | None = None  # openapi-cn01.tange365.com

    async def discover_service(self) -> dict[str, str]:
        """Discover region-specific API endpoints. 失败时回退默认 cn01（ep.tange365.com 常不可达）。"""
        default = {
            "api": "https://api-cn01.tange365.com",
            "open_api": "https://openapi-cn01.tange365.com",
        }
        try:
            payload = {
                "source": "app",
                "deviceid": f"s_{APP_PKGNAME}_{APP_VERSION}",
                "appstore": "default",
                "pkgname": APP_PKGNAME,
                "version": APP_VERSION,
            }
            async with self._session.post(
                EP_SERVICE_URL,
                json=payload,
                headers={"content-type": "application/json; charset=UTF-8"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if data.get("code") != 200:
                    raise YingTengApiError(f"Service discovery failed: {data.get('msg', 'unknown')}")
                result = data["data"]
                self._api_base = result["api"].rstrip("/")
                self._open_api_base = result["open_api"].rstrip("/")
                _LOGGER.debug("Service discovery: api=%s, open_api=%s", self._api_base, self._open_api_base)
                return result
        except Exception as err:
            _LOGGER.warning("Service discovery failed (%s), fallback to cn01 endpoints", err)
        self._api_base = default["api"]
        self._open_api_base = default["open_api"]
        return default

    async def login(self, username: str, password: str) -> dict[str, Any]:
        """Login with username and password."""
        if not self._open_api_base:
            await self.discover_service()

        payload = {
            "username": username,
            "pwd": password,
            "area_code": "86",
            "login_type": "pwd",
        }
        headers = {**DEFAULT_HEADERS, "content-type": "application/json; charset=utf-8"}
        url = f"{self._open_api_base}/v2/user/login"

        async with self._session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengAuthError(data.get("msg", "Login failed"))
            result = data["data"]
            self._access_token = result["access_token"]
            self._refresh_token = result.get("refresh_token")
            self._user_id = result["user_id"]
            _LOGGER.debug("Login success: user_id=%s", self._user_id)
            return data

    @property
    def auth_headers(self) -> dict[str, str]:
        """Headers with authorization token."""
        return {**DEFAULT_HEADERS, "authorization": f"Bearer {self._access_token}"}

    async def ensure_auth(self) -> None:
        """Re-login if token expired and credentials are available."""
        if self._username and self._password:
            await self.login(self._username, self._password)

    async def get_device_list(self) -> list[dict[str, Any]]:
        """Get device list from api-cn01 endpoint (simpler validation)."""
        if not self._access_token or not self._user_id:
            raise YingTengAuthError("Not logged in")

        url = f"{self._api_base}/app/device/list/least"
        payload = {
            "country_code": "CN",
            "language": "zh",
            "user_id": self._user_id,
            "token": self._access_token,
            "version": APP_VERSION,
        }
        headers = {**self.auth_headers, "content-type": "application/json; charset=utf-8"}

        async with self._session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengApiError(f"Device list failed: {data.get('msg', 'unknown')}")
            return data["data"]

    async def get_user_info(self) -> dict[str, Any]:
        """Get user info."""
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")
        url = f"{self._open_api_base}/v2/user"
        async with self._session.get(url, headers=self.auth_headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengApiError(f"User info failed: {data.get('msg', 'unknown')}")
            return data["data"]

    def _common_payload(self, device_id: str) -> dict[str, Any]:
        """Build the common request body used by thumbnail/online endpoints.

        Captured from real app traffic (2026-07-12): these endpoints require the
        full set of x-tg-* derived fields plus a raw JWT ``token`` (not the
        ``Bearer``-prefixed one used in headers). ``device_id`` accepts a single
        id or comma-separated ids for batch queries.
        """
        return {
            "X-Tg-App-Sdk-Version": APP_SDK_VERSION,
            "X-Tg-Sdk-Version": APP_SDK_VERSION,
            "app_version_no": "",
            "appid": APP_ID,
            "appstore": "default",
            "country_code": "CN",
            "device_id": device_id,
            "language": "zh-cn",
            "pkgname": APP_PKGNAME,
            "platform": "android",
            "token": self._access_token,
            "version": APP_VERSION,
            "version_no": APP_SDK_VERSION,
        }

    async def fetch_cloud_records(
        self, device_id: str, date: str
    ) -> dict[str, Any]:
        """查询云端录像列表（v2/cloud/videos/{dev}/{date}）。

        date 格式: YYYY-MM-DD
        返回: {"des_key": str, "items": [{"start_time": int, "end_time": int, "ossid": str}]}
        """
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")
        url = f"{self._open_api_base}/v2/cloud/videos/{device_id}/{date}"
        headers = self.auth_headers
        async with self._session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengApiError(
                    f"Cloud records failed: {data.get('msg', 'unknown')}"
                )
            return data.get("data") or {}

    async def fetch_alarm_messages(
        self,
        device_id: str,
        date: str = "",
        tag: str | None = None,
        offset: int = 0,
        limit: int = 30,
    ) -> dict[str, Any]:
        """查询告警消息列表（POST v2/cloud/event）。

        App 逆向（jadx14 CloudMessage.queryMessageByDate）确认。
        tag 可选: motion(移动) / body(人形) / car(车辆) / pet(宠物) / sound(声音) / ai_summary(AI哨兵)
        返回: {"items": [{id, ossid, thumbnail, image, time, can_play, tag, ossid_video, summary}]}
        id 格式: "{deviceId}:{tag}:{unix_ts}"
        """
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")
        url = f"{self._open_api_base}/v2/cloud/event"
        payload = {
            "device_id": device_id,
            "date": date or datetime.now().strftime("%Y-%m-%d"),
            "offset": offset,
            "limit": limit,
        }
        if tag:
            payload["tag"] = tag
        headers = {**self.auth_headers, "content-type": "application/json; charset=utf-8"}
        async with self._session.post(
            url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengApiError(
                    f"Alarm messages failed: {data.get('msg', 'unknown')}"
                )
            return data.get("data") or {}

    async def fetch_message_categories(self, device_id: str) -> list[dict[str, Any]]:
        """查询告警消息分类（POST v2/cloud/filter）。

        返回: [{tag, name, type}] 如 [{tag:"motion",name:"发现移动",type:"ipc"},
              {tag:"body",name:"发现人形",type:"ai"}, ...]
        """
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")
        url = f"{self._open_api_base}/v2/cloud/filter"
        headers = {**self.auth_headers, "content-type": "application/json; charset=utf-8"}
        async with self._session.post(
            url, json={"device_id": device_id}, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengApiError(
                    f"Message categories failed: {data.get('msg', 'unknown')}"
                )
            return data.get("data") or []

    async def get_push_switches(self, device_id: str) -> dict[str, Any]:
        """查询推送开关配置（GET v2/cloud/switcher/{dev}）。

        返回: {"global_status": bool, "item": [{tag, name, status}],
               "undisturbed": {"status": bool, "start": "00:00", "end": "07:00"}}
        """
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")
        url = f"{self._open_api_base}/v2/cloud/switcher/{device_id}"
        async with self._session.get(
            url, headers=self.auth_headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengApiError(
                    f"Push switches failed: {data.get('msg', 'unknown')}"
                )
            return data.get("data") or {}

    async def update_push_switches(
        self, device_id: str, configure: dict[str, Any]
    ) -> bool:
        """更新推送开关配置（POST v2/cloud/switcher/{dev}）。

        configure 结构:
          {"global_status": bool, "item": [{"tag": "motion", "status": true}, ...],
           "undisturbed": {"status": bool, "start": "00:00", "end": "07:00"}}
        """
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")
        url = f"{self._open_api_base}/v2/cloud/switcher/{device_id}"
        headers = {**self.auth_headers, "content-type": "application/json; charset=utf-8"}
        async with self._session.post(
            url, json=configure, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengApiError(
                    f"Update push switches failed: {data.get('msg', 'unknown')}"
                )
            return True

    async def share_generate_code(self, device_id: str, timeout_s: int = 600) -> dict[str, Any]:
        """生成设备共享码（GET /v2/share/code/{dev}/{timeout}）。

        返回: {"code": "SC://xxxxxxxx"}
        """
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")
        url = f"{self._open_api_base}/v2/share/code/{device_id}/{timeout_s}"
        async with self._session.get(
            url, headers=self.auth_headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengApiError(f"Share code failed: {data.get('msg', 'unknown')}")
            return data.get("data") or {}

    async def share_list(self, device_id: str) -> list[dict[str, Any]]:
        """查询设备共享列表（GET /v2/share/{dev}）。

        返回: {"items": [{share_id, share_time, share_type, nickname, user_id}], "total": n}
        """
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")
        url = f"{self._open_api_base}/v2/share/{device_id}"
        async with self._session.get(
            url, headers=self.auth_headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengApiError(f"Share list failed: {data.get('msg', 'unknown')}")
            return data.get("data") or {}

    async def share_cancel(self, device_id: str, share_id: int, share_type: int = 1) -> bool:
        """取消设备共享（POST /v2/share/cancel/{dev}）。"""
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")
        url = f"{self._open_api_base}/v2/share/cancel/{device_id}"
        headers = {**self.auth_headers, "content-type": "application/json; charset=utf-8"}
        async with self._session.post(
            url,
            params={"share_id": share_id, "share_type": share_type},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengApiError(f"Share cancel failed: {data.get('msg', 'unknown')}")
            return True

    async def get_cloud_oss_token(self, device_id: str, ossid: str) -> dict[str, Any]:
        """获取 OSS 对象存储访问凭证（GET /v2/cloud/oss-token-by-user/{dev}?oss_id=）。

        App 逆向（jadx14 ObjectStorageService.a()）确认：
        - 之前误用的 POST /v2/cloud/oss 实际不存在（404）
        - 正确接口为 GET，路径 /v2/cloud/oss-token-by-user/{device_id}，query 带 oss_id
        返回 StorageAccessToken:
        {access_key_id, access_key_secret, security_token, expiration_int,
         bucket, end_point, region_id, root_path, platform, ossid, url}
        """
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")
        url = f"{self._open_api_base}/v2/cloud/oss-token-by-user/{device_id}"
        headers = self.auth_headers
        async with self._session.get(
            url,
            params={"oss_id": ossid},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                raise YingTengApiError(
                    f"OSS token failed: {data.get('msg', 'unknown')}"
                )
            return data.get("data") or {}

    async def get_device_thumbnail(self, uuid: str) -> bytes | None:
        """Get a still image for a device via the thumbnail API + signed OSS URL."""
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")

        url = f"{self._open_api_base}/v2/device/thumbnail"
        payload = self._common_payload(uuid)
        headers = {**self.auth_headers, "content-type": "application/json; charset=utf-8"}

        async with self._session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("code") != 200:
                _LOGGER.debug("Thumbnail API failed: %s", data.get("msg"))
                return None
            device_data = data["data"].get(uuid, {})
            thumb_url = device_data.get("image_path")
            if not thumb_url:
                return None

        # Download the signed OSS image (Expires ~1h, re-fetched each call)
        try:
            async with self._session.get(thumb_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception as err:
            _LOGGER.debug("Failed to download thumbnail: %s", err)
        return None

    async def check_devices_online(self, devices: list[dict]) -> list[dict]:
        """Check online status for all devices via the /v2/device/online API.

        The captured request sends comma-separated device ids in one call and
        returns ``is_online`` per device.
        """
        if not self._open_api_base:
            raise YingTengAuthError("Not logged in")

        url = f"{self._open_api_base}/v2/device/online"
        device_ids = ",".join(d["uuid"] for d in devices)
        payload = self._common_payload(device_ids)
        headers = {**self.auth_headers, "content-type": "application/json; charset=utf-8"}

        try:
            async with self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if data.get("code") == 200:
                    for device in devices:
                        uuid = device["uuid"]
                        online_info = data["data"].get(uuid, {})
                        device["online"] = online_info.get("is_online", False)
                        device["ip"] = online_info.get("client_addr")
                return devices
        except Exception as err:
            _LOGGER.debug("Online check failed: %s", err)
            for device in devices:
                device["online"] = False
            return devices

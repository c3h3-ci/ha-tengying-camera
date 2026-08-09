#!/usr/bin/env python3
"""腾影智联 addon 凭据刷新脚本（标准库实现，无第三方依赖）
用法: fetch_creds.py <username> <password> <device_id>
输出: p2pid=... / pwd=... / init=... （一行一个）
"""
import base64
import json
import sys
import urllib.request

APPID = "AD_2eVJIyCyBkZQ93IJt9XO6fRyE5A"
KEY = bytes([0xF0, 0xE1, 0xD2, 0xC3, 0xB4, 0xA5, 0x96, 0x87])
API = "https://openapi-cn01.tange365.com"


def tg_headers(token=None):
    h = {
        "x-tg-app-id": APPID,
        "x-tg-app-pkgname": "com.yingteng.ipc",
        "x-tg-app-platform": "android",
        "x-tg-platform": "android",
        "x-tg-app-sdk-version": "21903",
        "x-tg-sdk-version": "21903",
        "x-tg-app-version": "2.4.0",
        "x-tg-app-store": "default",
        "accept-language": "zh-cn",
        "content-type": "application/json; charset=utf-8",
        "user-agent": "okhttp/4.10.0",
    }
    if token:
        h["authorization"] = token
    return h


def post(url, payload, token=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=tg_headers(token), method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def decrypt(b64):
    raw = base64.b64decode(b64)
    return bytes(b ^ KEY[i % 8] for i, b in enumerate(raw)).decode("utf-8", errors="replace")


def main():
    if len(sys.argv) < 4:
        print("usage: fetch_creds.py <username> <password> <device_id>", file=sys.stderr)
        sys.exit(1)
    username, password, dev = sys.argv[1], sys.argv[2], sys.argv[3]
    login = post(
        f"{API}/v2/user/login",
        {"username": username, "pwd": password, "area_code": "86", "login_type": "pwd"},
    )
    token = login["data"]["access_token"]
    payload = {
        "X-Tg-App-Sdk-Version": "21903",
        "X-Tg-Sdk-Version": "21903",
        "app_version_no": "",
        "appid": APPID,
        "appstore": "default",
        "country_code": "CN",
        "device_id": dev,
        "language": "zh-cn",
        "pkgname": "com.yingteng.ipc",
        "platform": "android",
        "token": token,
        "version": "2.4.0",
        "version_no": "21903",
    }
    conn = post(f"{API}/v2/device/connection", payload, token)
    data = conn.get("data", {})
    blob = data.get(dev) if isinstance(data, dict) else None
    if not blob and isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and len(v) > 300:
                blob = v
    if not blob:
        print("no blob", file=sys.stderr)
        sys.exit(1)
    plain = decrypt(blob)
    outer = json.loads(plain)
    ppcs = json.loads(outer["ppcs"]) if isinstance(outer.get("ppcs"), str) else outer.get("ppcs")
    p2pid = ppcs.get("p2pid", "")
    pwd = outer.get("pwd", "")
    initstring = ppcs.get("initstring", "").replace("ppcs:", "")
    if not (p2pid and pwd and initstring):
        print("missing creds", file=sys.stderr)
        sys.exit(1)
    print(f"p2pid={p2pid}")
    print(f"pwd={pwd}")
    print(f"init={initstring}")


if __name__ == "__main__":
    main()

"""Constants for Tengying Smart Camera integration."""

DOMAIN = "tengying_camera"
MANUFACTURER = "森亦杨科技"

# Service endpoints
EP_SERVICE_URL = "https://ep.tange365.com/service"

# App credentials (from APK decompile)
APP_ID = "AD_2eVJIyCyBkZQ93IJt9XO6fRyE5A"
APP_PKGNAME = "com.yingteng.ipc"
APP_VERSION = "2.4.0"
APP_SDK_VERSION = "21903"

# Default headers shared across all API calls
DEFAULT_HEADERS = {
    "x-tg-app-id": APP_ID,
    "x-tg-app-pkgname": APP_PKGNAME,
    "x-tg-app-platform": "android",
    "x-tg-platform": "android",
    "x-tg-app-sdk-version": APP_SDK_VERSION,
    "x-tg-sdk-version": APP_SDK_VERSION,
    "x-tg-app-version": APP_VERSION,
    "x-tg-app-store": "default",
    "accept-language": "zh-cn",
    "user-agent": "okhttp/4.10.0",
}

# Default polling interval (seconds)
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 10

# RTSP bridge (tutk-bridge addon) host:port, e.g. "127.0.0.1:8554"
CONF_RTSP_HOST = "rtsp_host"

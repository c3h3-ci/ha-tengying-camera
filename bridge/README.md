# tengying_bridge — 收流桥构建说明

腾影摄像头 PPCS 收流桥：`bridge_ppcs.c`（NDK 交叉编译）→ bionic chroot 运行厂商 `libPPCS_API.so` → HEVC 裸流推给 mediamtx RTSP。

## 快速部署（推荐：直接用 ghcr 镜像）

```bash
docker run -d --name tengying_bridge \
  --privileged --network host --restart unless-stopped \
  -v /path/to/options.json:/data/options.json \
  ghcr.io/c3h3-ai/tengying-bridge:0.5.0
```

镜像已含：bridge 二进制 + bionic rootfs + 厂商 libPPCS_API.so + mediamtx + ffmpeg + 凭据刷新脚本。

## 自建镜像（需要逆向产物）

镜像内容依赖 APK 提取的私有文件，自建需准备：
- `libPPCS_API.so`（arm64，从 APK lib/arm64-v8a/ 提取）
- bionic rootfs（linker64 + libc 等，从 Android 模拟器镜像提取）
- `mediamtx` 二进制（官方 release）

准备齐后：

```bash
# 1. NDK 交叉编译 bridge（Windows）
aarch64-linux-android24-clang -O2 -o bridge_bionic bridge_ppcs.c

# 2. 布置构建上下文（参考 Dockerfile 目录结构）
#    rootfs_system/  bridge_bionic  mediamtx  mediamtx.yml
#    fetch_creds.py  options.json   run.sh

# 3. 构建
docker build --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest \
  -t tengying_bridge:local .

# 4. 运行
docker run -d --name tengying_bridge --privileged --network host \
  --restart unless-stopped tengying_bridge:local
```

## bridge 命令行参数

```
./bridge_ppcs <p2pid> <pwd> <initstring> [mode] [ctrl_port] [audio_down_port]
  mode            0=禁 LAN 搜索走云中继（异地必须）
  ctrl_port       PTZ/指令控制端口（127.0.0.1:8561+idx）
  audio_down_port 音频下行推送端口（127.0.0.1:8661+idx，双向语音）
```

控制通道协议（TCP JSON）：
```json
{"io": 4097, "payload": "010000000064"}      // PTZ/任意 IOCTRL
{"io": 32792, "payload": "000000000200000000000000", "wait": 1}  // 查询等响应
{"audio": "<SFrameInfo+G711A hex>"}          // 语音上行（ch5）
```

## run.sh 说明

- 每设备独立管道：`fetch_creds.py`（登录刷凭据）→ `bridge | ffmpeg push` → mediamtx RTSP
- 凭据轮换已内置（每次启动/设备断线自动刷新）
- 单设备管道退出 5s 独立重启，不影响其他设备
- 端口映射：设备 idx 0 → ctrl 8561 + audio 8661；idx 1 → 8562 + 8662；RTSP `tengying_{uuid}`

## 完整协议文档

见 `../docs/TECHNICAL_DOCUMENT.md`（协议逆向、帧格式、OSS 签名、DES 解密、双向语音等全部细节）。

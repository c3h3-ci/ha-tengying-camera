# 影腾智联 (Tengying Camera) — Home Assistant 集成

> 将「影腾智联」App（com.yingteng.ipc）的摄像头能力完整复刻到 Home Assistant。
> 支持**异地远程**监控（走云中继，无需局域网直连）。

## 功能一览

| 功能 | 说明 |
|---|---|
| 📹 多设备实时直播 | 1080p HEVC 双流 RTSP（云中继） |
| 🎥 PTZ 云台 | 8 方向 + 速度 + 自动停止 |
| ☁️ 云端录像 | 列表查询 → 下载 → DES 解密 → 带声音 mp4 → `/local/` 直接播 |
| 🚨 告警消息 + AI 识别 | 移动/人形/车辆/宠物/声音事件，最新告警 sensor |
| 🔔 推送开关 | 按事件类型开关云端推送 |
| 🌙 设备设置 | 红外夜视 / 移动跟踪（switch 实体） |
| 💾 SD 卡回放 | 按时间点回放，自动恢复直播 |
| 👨👩👧 设备共享 | 生成共享码，App 内添加 |
| 🗣️ 双向语音 | 喊话（ch5 上行）+ 听现场（ch1 下行采集） |

## 架构

```
摄像头(爸妈家) ──PPCS 云中继──▶ tengying_bridge 容器 ──RTSP──▶ HA camera 实体
                                        │
                          TCP ctrl 8561+idx (PTZ/设置/回放/喊话)
                          TCP audio 8661+idx (听现场)
HA 集成 ──HTTPS 云 API──▶ 腾影云 (登录/告警/录像/共享/OSS)
```

- **bridge 镜像**（含 bionic chroot + 厂商 P2P 库）：`ghcr.io/c3h3-ai/tengying-bridge:0.5.0`
- **HA 集成**：`custom_components/tengying_camera/`（14 个服务 + 5 类实体）

## 安装

本集成由两部分组成：

1. **bridge Add-on**（Supervisor 一键安装）— 跑 PPCS 收流 + RTSP，推荐用此方式部署。
2. **HA 集成**（HACS）— 提供实体与服务，调用云端 API。

### 方式一：Supervisor Add-on（推荐）

1. **添加 Add-on 仓库**：设置 → 加载项 → 加载项商店 → 右上 ⋮ → 仓库 → 填入
   `https://github.com/C3H3-AI/ha-tengying-camera` → 添加 → 等待重载。
2. **安装 Tengying Camera Bridge**：在加载项商店搜「Tengying Camera Bridge」→ 安装。
3. **配置选项**（安装后「配置」页）：

   | 选项 | 说明 |
   |---|---|
   | `username` | 腾影账号 |
   | `password` | 腾影密码 |
   | `devices` | 设备 UUID 数组，如 `["YT3586ZENZ3B"]`（顺序决定控制端口映射） |
   | `rtsp_port` | RTSP 端口，默认 `8560` |

4. **启动**：开启「自动启动」「自动更新」→ 启动。日志见「日志」页，应出现
   `[main] all device pipelines started`。

> 镜像 `ghcr.io/c3h3-ai/tengying-bridge:0.5.0` 需为 **public**（仓库默认私有，
> 请到 GitHub Packages 页面手动将包可见性改为 Public，否则 Supervisor 拉取会 403）。
> 容器需要 `特权模式` + `host 网络`（Add-on 已在 `config.yaml` 声明）。

### 方式二：手动 docker run（高级 / 替代）

```bash
docker run -d --name tengying_bridge \
  --privileged --network host --restart unless-stopped \
  -v /path/to/options.json:/data/options.json \
  ghcr.io/c3h3-ai/tengying-bridge:0.5.0
```

`options.json`（参考 `bridge/options.example.json`）：

```json
{
  "username": "你的腾影账号",
  "password": "你的密码",
  "devices": ["设备UUID1", "设备UUID2"],
  "rtsp_port": 8560
}
```

> 设备 UUID 可在 App 设备详情或 `list_records` 服务返回中看到。
> 容器需要 `--privileged`（chroot 挂载）+ `--network host`（RTSP/控制端口）。

### 安装 HA 集成（HACS 或手动）

**HACS 方式**：添加自定义仓库 `https://github.com/C3H3-AI/ha-tengying-camera`（类别：Integration）→ 安装。

**手动方式**：把 `custom_components/tengying_camera/` 拷到 `/config/custom_components/`，重启 HA。

### 配置

设置 → 设备与服务 → 添加集成 → 搜「影腾智联」→ 填账号密码 + RTSP 地址（`127.0.0.1:8560`）+ 设备顺序。

## 服务速查（14 个）

`ptz` · `list_records` · `download_record` · `list_messages` · `list_message_categories` · `get_push_switches` · `set_push_switch` · `device_command` · `record_play` · `share_code` · `share_list` · `share_cancel` · `talkback` · `audio_down`

```yaml
# 云台
service: tengying_camera.ptz
data: {device_id: YT3586ZENZ3B, direction: right_up, speed: 100, duration: 500}

# 下载今天录像
service: tengying_camera.download_record
data: {device_id: YT3586ZENZ3B, date: "2026-08-09"}

# 人形告警（AI 识别）
service: tengying_camera.list_messages
data: {device_id: YT3586ZENZ3B, tag: body}

# 喊话
service: tengying_camera.talkback
data: {device_id: YT3586ZENZ3B, file: /config/www/tts/hello.mp3}

# 听现场（10 秒）
service: tengying_camera.audio_down
data: {device_id: YT3586ZENZ3B, duration: 10}
```

完整协议细节与实测记录见 [docs/TECHNICAL_DOCUMENT.md](docs/TECHNICAL_DOCUMENT.md)。

## 常见问题

- **摄像头看不到**：确认 `options.json` 的账号/设备 UUID 正确，容器日志 `/tmp/bridge_*.log` 无 `creds FAILED`。
- **PTZ/设置无效**：确认设备在 `devices` 数组中的顺序与集成配置一致（决定控制端口映射）。
- **视频流卡顿**：云中继带宽取决于家庭上行，1080p HEVC 约需 2-4 Mbps。

## 免责声明

本项目为**个人逆向研究**产物，基于「影腾智联」App 的网络协议与 APK 反编译实现，**与厂商无任何关联**。bridge 镜像内含 APK 提取的厂商动态库（libPPCS_API.so），仅限个人学习/自用，请勿商用或公开传播。

## License

MIT

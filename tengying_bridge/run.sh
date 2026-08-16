#!/bin/bash
# tengying_bridge addon 启动脚本 v8（不 chroot + HEVC NAL 过滤 + 末帧保持代理）
#
# 关键修复 (v8): 影腾相机经云中继间歇推流 —— 推流窗口(~150s)到期相机停推, bridge 等 90s 无数据后
#   才 reconnect。停推期间 ffmpeg 收不到数据 -> mediamtx 的 RTSP publisher 掉线 -> HA 黑屏。
#   修复: 在 filter_hevc.py 与 ffmpeg 之间插入 repeater.py（HEVC Annex-B 末帧保持代理）:
#   实时透传 NAL, 缓冲最近一个 GOP; stdin 超过 REPEATER_STALL_SEC(默认4s) 无数据时循环重放该 GOP,
#   使 RTSP publisher 始终在线, HA 显示"冻结的最后一帧"而非黑屏。相机恢复推流后自动切回实时。
#   零侵入: 不改 bridge 二进制。
#
# 关键修复 (v6): HA Supervisor addon 容器禁止 mount syscall
#   (mount ... failed: Permission denied，即使 privileged:[SYS_ADMIN] + Seccomp:0 仍被容器运行时策略拦截)。
#   旧版 run.sh 用 chroot + mount -t proc/--bind /dev /sys，导致 chroot 内 /proc /dev /sys 为空，
#   bridge_bionic 在加载阶段即 Segmentation fault (core dumped)，ffmpeg 收空管道、mini 端口不监听。
# 修复: 不 chroot，直接用 bionic linker64 + LD_LIBRARY_PATH 跑 bridge；
#       bridge 使用宿主(Alpine)容器真实的 /proc /dev，加载正常、能建立 PPCS 会话。
#
# 关键修复 (v7): bridge 在 stdout 视频流开头会多吐一个 11 字节的 "type 0" 垃圾 NAL
#   (bytes: 00 00 01 00 00 8e 37 02 00 aa 21 df 07 00 00，HEVC 规范里 type 0 是 reserved/invalid)。
#   ffmpeg 的 -f hevc 解封装器遇到这个开头会抽不出 VPS/SPS/PPS extradata，进而丢弃全部帧
#   ("Failed to parse header of NALU (type 0)" / "missing picture in access unit")，RTSP 虽 online 却无画面。
#   修复: 在 bridge 与 ffmpeg 之间插入 filter_hevc.py，流式剔除 type-0 NAL，ffmpeg 直接看到干净的
#   Annex-B 流（首帧即 VPS），正常解析 1920x1080@25fps 并推 RTSP。该垃圾 NAL 仅在每次连接开头出现一次。
#
# bridge 仅接受 4 个参数: <p2pid> <pwd> <initstring> [mode]
#   (旧版传的第 5/6/7 个端口参数被 bridge 完全忽略；bridge 内部自行监听 ctrl/audio/mini 固定端口)
#   mode=0 云中继 / mode=1 LAN 直连（与设备同网段时可用）
#
# 主镜头视频(ch=2)由 bridge 输出到 stdout，经 filter_hevc.py 剔除垃圾 NAL 后，管道给 ffmpeg 推 RTSP。
CONFIG_PATH=/data/options.json

USERNAME=$(python3 -c "import json;print(json.load(open('$CONFIG_PATH'))['username'])" 2>/dev/null || echo "")
PASSWORD=$(python3 -c "import json;print(json.load(open('$CONFIG_PATH'))['password'])" 2>/dev/null || echo "")
RTSP_PORT=$(python3 -c "import json;print(json.load(open('$CONFIG_PATH'))['rtsp_port'])" 2>/dev/null || echo "8560")

DEVICES=$(python3 -c "
import json
d = json.load(open('$CONFIG_PATH'))
devs = d.get('devices') or ([d['device_id']] if d.get('device_id') else [])
print(' '.join(devs))
" 2>/dev/null || echo "")

if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ] || [ -z "$DEVICES" ]; then
    echo "[start] ERROR: options.json 缺少 username/password/devices"
    echo "  示例: {username, password, devices: [uuid1, uuid2], rtsp_port: 8560}"
    exit 1
fi

echo "[start] devices=[$DEVICES] rtsp_port=$RTSP_PORT"

# bridge 运行环境（不 chroot，使用宿主 /proc /dev）
export LD_LIBRARY_PATH=/opt/bionic/system/lib64
LINKER=/opt/bionic/system/bin/linker64
BRIDGE=/opt/bionic/data/bridge_bionic

# 信号处理
trap 'echo "[stop] signal received"; kill $MTX_PID 2>/dev/null; pkill -f bridge_bionic 2>/dev/null; exit 0' TERM INT

# 启动 mediamtx（RTSP server，持续消费 push 流）
/opt/mediamtx/mediamtx /opt/mediamtx/mediamtx.yml > /tmp/mediamtx.log 2>&1 &
MTX_PID=$!
sleep 2

# 每设备一个管道：刷新凭据 -> bridge 收流(stdout HEVC) | ffmpeg push RTSP
# 管道退出后 5s 单独重启，不影响其他设备
spawn_device() {
    local DEV="$1"
    local IDX="$2"
    local P2PID PWD INIT
    echo "[dev:$DEV] fetching creds..."
    if ! python3 /opt/bridge/fetch_creds.py "$USERNAME" "$PASSWORD" "$DEV" > "/tmp/creds_$DEV.txt" 2>"/tmp/creds_$DEV.err"; then
        echo "[dev:$DEV] creds FAILED"
        cat "/tmp/creds_$DEV.err"
        return 1
    fi
    P2PID=$(grep '^p2pid=' "/tmp/creds_$DEV.txt" | cut -d= -f2)
    PWD=$(grep '^pwd=' "/tmp/creds_$DEV.txt" | cut -d= -f2)
    INIT=$(grep '^init=' "/tmp/creds_$DEV.txt" | cut -d= -f2)
    echo "[dev:$DEV] p2pid=$P2PID pwd_len=${#PWD} init_len=${#INIT}"

    while true; do
        echo "[dev:$DEV] starting pipeline -> rtsp://127.0.0.1:$RTSP_PORT/tengying_$DEV"
        "$LINKER" "$BRIDGE" "$P2PID" "$PWD" "$INIT" 0 2>>"/tmp/bridge_$DEV.log" \
            | python3 /opt/bridge/filter_hevc.py 2>>"/tmp/filter_$DEV.log" \
            | python3 /opt/bridge/repeater.py 2>>"/tmp/repeater_$DEV.log" \
            | ffmpeg -hide_banner -loglevel warning -fflags +genpts -f hevc -i pipe:0 -c copy \
              -f rtsp -rtsp_transport tcp "rtsp://127.0.0.1:$RTSP_PORT/tengying_$DEV" 2>>"/tmp/ffmpeg_$DEV.log"
        echo "[dev:$DEV] pipeline ended, restarting in 5s"
        sleep 5
    done &
    return 0
}

# 启动所有设备管道
IDX=0
for dev in $DEVICES; do
    spawn_device "$dev" "$IDX"
    IDX=$((IDX + 1))
done

echo "[main] all device pipelines started, waiting..."
wait

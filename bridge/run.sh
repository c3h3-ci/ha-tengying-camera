#!/bin/bash
# tengying_bridge addon 启动脚本 v4（多设备支持）
# 流程: 挂载伪文件系统 -> 刷新凭据 -> 起 mediamtx -> 每设备独立 bridge|ffmpeg push 管道
# 关键: bridge 第4参数 mode=0（禁 LAN 搜索，走云中继）
CONFIG_PATH=/data/options.json

USERNAME=$(python3 -c "import json;print(json.load(open('$CONFIG_PATH'))['username'])" 2>/dev/null || echo "13800138000")
PASSWORD=$(python3 -c "import json;print(json.load(open('$CONFIG_PATH'))['password'])" 2>/dev/null || echo "YOUR_PASSWORD_HERE")
RTSP_PORT=$(python3 -c "import json;print(json.load(open('$CONFIG_PATH'))['rtsp_port'])" 2>/dev/null || echo "8560")

# 设备列表：优先 devices 数组，兼容旧 device_id
DEVICES=$(python3 -c "
import json
d = json.load(open('$CONFIG_PATH'))
devs = d.get('devices') or ([d['device_id']] if d.get('device_id') else [])
print(' '.join(devs))
" 2>/dev/null || echo "YT3486ZCX35W")

echo "[start] devices=[$DEVICES] rtsp_port=$RTSP_PORT"

# 1. 挂载 bionic chroot 所需伪文件系统
mkdir -p /opt/bionic/proc /opt/bionic/dev /opt/bionic/sys
mount -t proc proc /opt/bionic/proc 2>/dev/null || echo "[mount] proc skip"
mount --bind /dev /opt/bionic/dev 2>/dev/null || echo "[mount] dev skip"
mount --bind /sys /opt/bionic/sys 2>/dev/null || echo "[mount] sys skip"

# 2. 信号处理
trap 'echo "[stop] signal received"; kill $MTX_PID 2>/dev/null; pkill -f bridge_bionic 2>/dev/null; exit 0' TERM INT

# 3. 启动 mediamtx（RTSP server，持续消费 push 流）
/opt/mediamtx/mediamtx /opt/mediamtx/mediamtx.yml > /tmp/mediamtx.log 2>&1 &
MTX_PID=$!
sleep 2

# 4. 每设备一个管道：刷新凭据 -> bridge 收流 | ffmpeg push RTSP
#    设备管道退出后 5s 单独重启（不影响其他设备）
#    每设备一个 PTZ 控制端口（8561 + 序号）
spawn_device() {
    local DEV="$1"
    local IDX="$2"
    local CTRL_PORT=$((8561 + IDX))
    local AUDIO_DOWN_PORT=$((8661 + IDX))
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
    echo "[dev:$DEV] p2pid=$P2PID pwd_len=${#PWD} init_len=${#INIT} ctrl_port=$CTRL_PORT"

    while true; do
        echo "[dev:$DEV] starting pipeline -> rtsp://127.0.0.1:$RTSP_PORT/tengying_$DEV"
        LD_LIBRARY_PATH=/system/lib64 chroot /opt/bionic /system/bin/linker64 /data/bridge_bionic \
            "$P2PID" "$PWD" "$INIT" 0 "$CTRL_PORT" "$AUDIO_DOWN_PORT" 2>>"/tmp/bridge_$DEV.log" \
            | ffmpeg -hide_banner -loglevel warning -fflags +genpts -f hevc -i pipe:0 -c copy \
              -f rtsp -rtsp_transport tcp "rtsp://127.0.0.1:$RTSP_PORT/tengying_$DEV" 2>>"/tmp/ffmpeg_$DEV.log"
        echo "[dev:$DEV] pipeline ended, restarting in 5s"
        sleep 5
    done &
    return 0
}

# 5. 启动所有设备管道
IDX=0
for dev in $DEVICES; do
    spawn_device "$dev" "$IDX"
    IDX=$((IDX + 1))
done

# 6. 主进程保持存活（子进程后台运行）
echo "[main] all device pipelines started, waiting..."
wait

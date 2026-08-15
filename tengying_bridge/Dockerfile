# tengying_bridge addon (self-contained local build)
# Supervisor 用本地源码构建，不依赖任何外部镜像 / ghcr
ARG BUILD_FROM
FROM ${BUILD_FROM}

# 依赖: python3 (凭据刷新, 仅标准库) + ffmpeg (H265 -> RTSP push) + bash/curl
RUN apk add --no-cache python3 py3-pip ffmpeg curl bash

# bionic rootfs（linker64 + libc 等，供 chroot 跑收流 so）
COPY rootfs_system/ /opt/bionic/system/

# bridge（收流程序）放入 chroot 的 /data 位置
COPY bridge_bionic /opt/bionic/data/bridge_bionic

# RTSP 服务器（mediamtx v1.20.0，已随仓库提交，完全自包含）
COPY mediamtx /opt/mediamtx/mediamtx
COPY mediamtx.yml /opt/mediamtx/mediamtx.yml

# 凭据刷新脚本 + 启动脚本
COPY fetch_creds.py /opt/bridge/fetch_creds.py
COPY run.sh /run.sh

RUN chmod +x /opt/mediamtx/mediamtx /opt/bionic/data/bridge_bionic \
    /opt/bionic/system/bin/linker64 /opt/bionic/system/lib64/*.so /run.sh

CMD ["/run.sh"]

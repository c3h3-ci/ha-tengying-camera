#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repeater.py — HEVC Annex-B 末帧保持代理 (last-frame-hold / GOP repeater)

放在 bridge 与 ffmpeg 之间:
    bridge | filter_hevc.py | repeater.py | ffmpeg -f hevc ...

问题背景:
    影腾相机经云中继间歇推流 —— 推流窗口(~150s)到期相机停推, bridge 等 90s 无数据后
    reconnect。停推期间 ffmpeg 收不到数据 -> mediamtx 的 RTSP publisher 掉线 -> HA 黑屏。

本脚本:
    1. 实时透传 HEVC NAL(带起始码)给 ffmpeg;
    2. 缓冲最近一个完整 GOP(从 IDR 到下一个 IDR 前);
    3. 当 stdin 超过 STALL_SEC 无数据(相机停推)时, 循环重放缓冲的 GOP 给 ffmpeg,
       使 RTSP publisher 始终在线, HA 显示"冻结的最后一帧"而非黑屏;
    4. stdin 恢复后自动切回实时。

零侵入: 不改 bridge 二进制, 纯 Python + 标准库。
"""
import os
import sys
import time
import select

# 起始码: 3 字节 00 00 01 (也兼容 4 字节 00 00 00 01)
SC3 = b"\x00\x00\x01"
STALL_SEC = float(os.environ.get("REPEATER_STALL_SEC", "4"))
REPLAY_INTERVAL = float(os.environ.get("REPEATER_INTERVAL", "1.0"))  # 每次重放间隔(秒), 约 1fps 冻结
DEBUG = os.environ.get("REPEATER_DEBUG", "") not in ("", "0", "false")

IDR_TYPES = (19, 20, 21)  # IDR / IDR-LP / CRA


def log(*a):
    if DEBUG:
        sys.stderr.write("[repeater] " + " ".join(str(x) for x in a) + "\n")
        sys.stderr.flush()


def nal_type_of(nal_body: bytes) -> int:
    if not nal_body:
        return -1
    return (nal_body[0] >> 1) & 0x3F


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    buf = b""          # 未处理字节
    gop = []           # 当前缓冲 GOP: 每个元素是带起始码的 NAL bytes
    have_idr = False
    last_input = time.time()

    def emit_nal(nal_with_sc: bytes):
        nonlocal have_idr
        # 记录 GOP
        body = nal_with_sc
        # 去掉起始码取 header 判断类型
        i = body.find(SC3)
        if i == -1:
            return
        hdr = body[i + 3:]
        t = nal_type_of(hdr)
        if t in IDR_TYPES:
            gop.clear()
            have_idr = True
        if have_idr:
            gop.append(nal_with_sc)
        # 实时透传
        stdout.write(nal_with_sc)
        stdout.flush()

    def replay():
        if not have_idr or not gop:
            return
        for nal in gop:
            stdout.write(nal)
        stdout.flush()

    while True:
        try:
            r, _, _ = select.select([stdin], [], [], STALL_SEC)
        except (ValueError, OSError):
            # stdin 关闭
            break

        if r:
            try:
                chunk = stdin.read(65536)
            except (ValueError, OSError):
                chunk = b""
            if not chunk:
                # EOF: bridge 退出, 进入重放直到进程被停
                log("stdin EOF, replaying last GOP")
                while True:
                    replay()
                    time.sleep(REPLAY_INTERVAL)
            buf += chunk
            last_input = time.time()
            # 按起始码切分 NAL
            while True:
                p = buf.find(SC3)
                if p == -1:
                    break
                # 找下一个起始码
                q = buf.find(SC3, p + 3)
                if q == -1:
                    # 当前 NAL 还不完整, 等更多数据
                    break
                nal = buf[p:q]
                buf = buf[q:]
                emit_nal(nal)
            # 若 buf 里残留(跨 chunk 的不完整 NAL), 保留到下次
        else:
            # 超时: 相机停推, 重放末帧 GOP
            replay()
            time.sleep(REPLAY_INTERVAL)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Streaming HEVC Annex-B NAL filter for the tengying_bridge addon.

The bridge_bionic binary emits a single 11-byte junk NAL of type 0
(reserved/invalid in HEVC) at the very start of its stdout video stream
(observed bytes: 00 00 01 00 00 8e 37 02 00 aa 21 df 07 00 00). This poisons
ffmpeg's `-f hevc` demuxer: it fails to extract VPS/SPS/PPS extradata and then
drops every frame ("Failed to parse header of NALU (type 0)").

This filter strips NAL units whose nal_unit_type == 0 (never used by a valid
HEVC encoder), passing a clean Annex-B stream through. It is streaming-safe:
it only holds back the currently-assembled NAL until the next start code is
seen, so latency is sub-frame.

Usage:  bridge ... | filter_hevc.py | ffmpeg -f hevc -i pipe:0 ...
"""
import sys

STDIN = sys.stdin.buffer
STDOUT = sys.stdout.buffer

SC = b"\x00\x00\x01"
CHUNK = 65536


def nal_type(header_pos: int) -> int:
    if 0 <= header_pos < len(buf):
        return (buf[header_pos] >> 1) & 0x3F
    return -1


buf = b""
nal_start = -1      # index in buf where the current NAL's 3-byte start code begins
search_from = 0

while True:
    chunk = STDIN.read(CHUNK)
    if not chunk:
        break
    buf += chunk
    i = buf.find(SC, search_from)
    while i != -1:
        if nal_start != -1:
            prev = buf[nal_start:i]
            t = nal_type(nal_start + 3) if len(prev) > 3 else -1
            if t != 0:                      # drop reserved/invalid type-0 NALs
                STDOUT.write(prev)
                STDOUT.flush()
        nal_start = i
        search_from = i + 3
        i = buf.find(SC, search_from)
    if nal_start > 0:
        # everything before the pending NAL is fully processed
        buf = buf[nal_start:]
        search_from = 0
        nal_start = 0

if nal_start != -1:
    last = buf[nal_start:]
    t = nal_type(3) if len(last) > 3 else -1
    if t != 0:
        STDOUT.write(last)
        STDOUT.flush()

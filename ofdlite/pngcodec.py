# -*- coding: utf-8 -*-
"""自己写的 PNG 编码器（不依赖 Pillow 或任何图像库）。

只做导出需要的部分：8 位 RGB / RGBA、无隔行。
按 PNG 规范（ISO/IEC 15948）实现：
  - 签名 8 字节
  - IHDR（宽、高、位深 8、颜色类型 2=RGB / 6=RGBA、压缩 0、滤波 0、隔行 0）
  - IDAT（zlib 压缩，每行前置一个滤波字节 0 = None）
  - IEND
  - CRC32 用 zlib.crc32

为什么不用 Pillow：项目原则是「能自己写的对照标准自己写」。
PNG 编码只有几百行，没有必要为它引入一个 14 MB 的二进制依赖。

复杂度 O(像素数)；主要耗时在 zlib.compress，用 level=6 与 Pillow 相当。
"""
from __future__ import annotations

import struct
import zlib
from typing import List, Sequence, Tuple

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    """一个 PNG 块：长度(4) + 类型(4) + 数据 + CRC(4)。CRC 覆盖类型+数据。"""
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode(width: int, height: int, pixels: bytes, channels: int = 3,
           level: int = 6) -> bytes:
    """把像素缓冲编码成 PNG 字节流。

    pixels 是行优先、无填充的字节串；channels = 3(RGB) 或 4(RGBA) 或 1(灰度)。
    """
    if channels not in (1, 2, 3, 4):
        raise ValueError("channels 只支持 1/2/3/4，收到 %r" % (channels,))
    color_type = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    stride = width * channels
    if len(pixels) != stride * height:
        raise ValueError("像素缓冲长度不对：期望 %d，实际 %d"
                         % (stride * height, len(pixels)))

    out = [SIGNATURE]
    out.append(_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                           8, color_type, 0, 0, 0)))

    # 每行前面加滤波类型字节 0（None）
    raw = bytearray(stride * height + height)
    src = memoryview(pixels)
    for y in range(height):
        raw[y * (stride + 1)] = 0
        raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)] = \
            src[y * stride:(y + 1) * stride]
    out.append(_chunk(b"IDAT", zlib.compress(bytes(raw), level)))
    out.append(_chunk(b"IEND", b""))
    return b"".join(out)


def write_file(path: str, width: int, height: int, pixels: bytes,
               channels: int = 3, level: int = 6) -> int:
    """写 PNG 文件，返回写入字节数。"""
    data = encode(width, height, pixels, channels, level)
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


# ------------------------------------------------------------------ 解码

def decode(data: bytes) -> Tuple[int, int, bytes, int]:
    """解 PNG（只支持 8 位、非隔行、无调色板）。

    返回 (width, height, pixels, channels)。OFD 里的图片资源多是这种形态。
    滤波按规范实现五种（None/Sub/Up/Average/Paeth）—— 少一种就会出花屏。
    """
    if not data.startswith(SIGNATURE):
        raise ValueError("不是 PNG")
    pos = len(SIGNATURE)
    width = height = depth = None
    channels = None
    color_type = None
    palette = b""
    idat = []
    while pos + 8 <= len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if tag == b"IHDR":
            (width, height, depth, color_type, _c, _f, inter) = \
                struct.unpack(">IIBBBBB", payload)
            if inter != 0:
                raise ValueError("不支持隔行 PNG")
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
            if channels is None:
                raise ValueError("不支持的颜色类型 %d" % color_type)
        elif tag == b"PLTE":
            palette = payload
        elif tag == b"IDAT":
            idat.append(payload)
        elif tag == b"IEND":
            break
    if width is None or channels is None:
        raise ValueError("PNG 缺 IHDR")
    if depth not in (1, 2, 4, 8):
        raise ValueError("不支持的色深 %d" % depth)

    raw = zlib.decompress(b"".join(idat))
    # 每行的**字节数**（低色深时按位打包）
    rowbytes = (width * channels * depth + 7) // 8
    # 滤波用的 bpp 是「一个像素占几个字节」，低色深时按 1 算
    bpp = max(1, (channels * depth) // 8)
    out = bytearray(width * height * channels)
    prev = bytearray(rowbytes)
    pos = 0
    for y in range(height):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + rowbytes])
        pos += rowbytes
        if ft == 1:        # Sub
            for i in range(bpp, rowbytes):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:      # Up
            for i in range(rowbytes):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:      # Average
            for i in range(rowbytes):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:      # Paeth
            for i in range(rowbytes):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pv = a + b - c
                pa, pb, pc = abs(pv - a), abs(pv - b), abs(pv - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif ft != 0:
            raise ValueError("未知滤波类型 %d" % ft)

        if depth == 8:
            out[y * width * channels:(y + 1) * width * channels] = line
        else:
            # 低色深：按位拆包成 8 位
            mask = (1 << depth) - 1
            scale = 255 // mask
            o = y * width * channels
            for x in range(width * channels):
                bit = x * depth
                byte = line[bit >> 3]
                shift = 8 - depth - (bit & 7)
                v = (byte >> shift) & mask
                out[o + x] = v * scale if depth < 8 else v
        prev = line

    if color_type == 3:
        # 调色板：索引 → RGB
        if not palette:
            raise ValueError("调色板 PNG 缺 PLTE")
        rgb = bytearray(width * height * 3)
        for i in range(width * height):
            idx = out[i]
            if idx * 3 + 2 < len(palette):
                rgb[i * 3] = palette[idx * 3]
                rgb[i * 3 + 1] = palette[idx * 3 + 1]
                rgb[i * 3 + 2] = palette[idx * 3 + 2]
        return width, height, bytes(rgb), 3
    return width, height, bytes(out), channels

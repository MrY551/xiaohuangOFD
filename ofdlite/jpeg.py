# -*- coding: utf-8 -*-
"""自己写的 JPEG 基线解码器（ISO/IEC 10918-1，SOF0）。

为什么自己写：GB/T 33190-2016 第 10 章不限定图片格式，JPEG 是必须支持的；
项目原则是「能自己写的对照标准自己写」。WPS 导出的真实 OFD 里就用 JPEG，
之前只有 PNG 解码器，那一页只剩文字。

覆盖范围：
  - 标记解析：SOI / APP0(APPn) / DQT / DHT / SOF0 / SOS / EOI
  - 精度 8 位、三/单分量、任意采样因子（1x1 / 2x1 / 1x2 / 2x2）
  - Huffman 解码（含 DC 差分、AC 的 EOB 与 ZRL）
  - 反 zigzag、反量化、分离式 IDCT（预计算余弦表）
  - 色度上采样到全分辨率、YCbCr → RGB

不做的：
  - 渐进式（SOF2）、无损（SOF3）、算术编码 —— 实测真实文件都是 SOF0，
    遇到其他帧类型会明确报错而不是解出坏图。
  - 重启标记（DRI/RSTn）支持了，但未在真实样本上验证过。
"""
from __future__ import annotations

import math
import struct

from . import _fast
from typing import Dict, List, Optional, Tuple

# 标准 zigzag 顺序（把解码后的自然序还原）
ZIGZAG = [
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
]


class JPEGError(Exception):
    pass


# ---------------------------------------------------------------- 余弦表

_COS = [[math.cos((2 * x + 1) * u * math.pi / 16.0) for x in range(8)]
        for u in range(8)]
# 归一化系数：u=0 时 1/sqrt2，其余 1
_C = [(1.0 / math.sqrt(2.0)) if u == 0 else 1.0 for u in range(8)]


def _idct8x8(block: List[int]) -> List[int]:
    """分离式 2D IDCT：先对每行做 1D，再对每列做 1D。

    比直接 4 重循环快 8 倍（1024 次乘加/块 vs 4096）。
    """
    COS = _COS
    C = _C
    tmp = [0.0] * 64
    # 行变换
    for v in range(8):
        base = v * 8
        for x in range(8):
            s = 0.0
            for u in range(8):
                s += C[u] * block[base + u] * COS[u][x]
            tmp[base + x] = s * 0.5
    out = [0] * 64
    # 列变换
    for x in range(8):
        for y in range(8):
            s = 0.0
            for v in range(8):
                s += C[v] * tmp[v * 8 + x] * COS[v][y]
            val = int(s * 0.5 + 128.0 + 0.5)
            out[y * 8 + x] = 0 if val < 0 else (255 if val > 255 else val)
    return out


# ---------------------------------------------------------------- 位读取

class _BitReader(object):
    """按位读；遇到 0xFF00 要跳过填充字节，0xFFD0-D7 是重启标记。"""

    def __init__(self, data: bytes, pos: int):
        self.data = data
        self.pos = pos
        self.buf = 0
        self.bits = 0
        self.eof = False

    def _fill(self) -> bool:
        if self.bits >= 8:
            return True
        if self.pos >= len(self.data):
            self.eof = True
            # 补 1 位，让调用方不至于死循环
            self.buf = 0
            self.bits = 8
            return False
        b = self.data[self.pos]
        self.pos += 1
        if b == 0xFF:
            # 可能是填充 0x00，也可能是标记
            if self.pos < len(self.data):
                nxt = self.data[self.pos]
                if nxt == 0x00:
                    self.pos += 1
                elif 0xD0 <= nxt <= 0xD7:
                    # 重启标记：吞掉，位流继续
                    self.pos += 1
                else:
                    # 真正的标记，停止
                    self.pos -= 1
                    self.eof = True
        self.buf = b
        self.bits = 8
        return True

    def read_bit(self) -> int:
        if self.bits == 0:
            if not self._fill():
                return 0
        self.bits -= 1
        return (self.buf >> self.bits) & 1

    def read_bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.read_bit()
        return v

    def align(self) -> None:
        self.bits = 0


# ---------------------------------------------------------------- Huffman

class _Huff(object):
    """一张 Huffman 表：用 (长度, 码值) 建查表。"""

    def __init__(self, counts: List[int], symbols: List[int]):
        self.map = {}          # {(length, code): symbol}
        self.max_len = 0
        code = 0
        k = 0
        for length in range(1, 17):
            for _ in range(counts[length - 1]):
                if k >= len(symbols):
                    break
                self.map[(length, code)] = symbols[k]
                k += 1
                code += 1
                if length > self.max_len:
                    self.max_len = length
            code <<= 1

    def decode(self, br: _BitReader) -> int:
        code = 0
        for length in range(1, self.max_len + 1):
            code = (code << 1) | br.read_bit()
            sym = self.map.get((length, code))
            if sym is not None:
                return sym
        return -1


def _extend(v: int, n: int) -> int:
    """把 n 位无符号值转成 JPEG 的有符号差分（负值用反码表示）。"""
    if n == 0:
        return 0
    if v < (1 << (n - 1)):
        return v - (1 << n) + 1
    return v


# ---------------------------------------------------------------- 主解码器

class JPEG(object):
    def __init__(self, data: bytes):
        self.data = data
        self.qt: Dict[int, List[int]] = {}
        self.ht_dc: Dict[int, _Huff] = {}
        self.ht_ac: Dict[int, _Huff] = {}
        self.width = 0
        self.height = 0
        self.precision = 8
        self.comps: List[dict] = []
        self.restart_interval = 0
        self.scan_start = 0
        self.sos_comps: List[dict] = []
        self._parse()

    # ------------------------------------------------------------ 标记
    def _parse(self) -> None:
        d = self.data
        if len(d) < 4 or d[0] != 0xFF or d[1] != 0xD8:
            raise JPEGError("不是 JPEG（缺 SOI）")
        i = 2
        n = len(d)
        while i < n - 1:
            if d[i] != 0xFF:
                i += 1
                continue
            m = d[i + 1]
            if m == 0xD8 or m == 0x01 or (0xD0 <= m <= 0xD7):
                i += 2
                continue
            if m == 0xD9:
                break
            if i + 4 > n:
                break
            ln = struct.unpack(">H", d[i + 2:i + 4])[0]
            seg = d[i + 4:i + 2 + ln]
            if m == 0xDB:
                self._parse_dqt(seg)
            elif m == 0xC4:
                self._parse_dht(seg)
            elif m in (0xC0, 0xC1):
                self._parse_sof(seg)
            elif m == 0xDD:
                if len(seg) >= 2:
                    self.restart_interval = struct.unpack(">H", seg[0:2])[0]
            elif m == 0xDA:
                self._parse_sos(seg)
                self.scan_start = i + 2 + ln
                self._scan_end = self._find_eoi(self.scan_start)
                return
            elif m in (0xC2, 0xC3, 0xC5, 0xC6, 0xC7):
                raise JPEGError(
                    "只支持基线 JPEG（SOF0），这个文件是 SOF%d（渐进式/无损），"
                    "未实现" % (m - 0xC0))
            i += 2 + ln
        if not self.comps:
            raise JPEGError("没有找到 SOF0 帧头")

    def _find_eoi(self, pos: int) -> int:
        d = self.data
        i = pos
        while i < len(d) - 1:
            if d[i] == 0xFF and d[i + 1] == 0xD9:
                return i
            i += 1
        return len(d)

    def _parse_dqt(self, seg: bytes) -> None:
        i = 0
        while i < len(seg):
            pq = seg[i] >> 4
            tq = seg[i] & 0x0F
            i += 1
            if pq == 0:
                tbl = list(seg[i:i + 64])
                i += 64
            else:
                tbl = [struct.unpack(">H", seg[i + 2 * k:i + 2 * k + 2])[0]
                       for k in range(64)]
                i += 128
            self.qt[tq] = tbl

    def _parse_dht(self, seg: bytes) -> None:
        i = 0
        while i < len(seg):
            tc = seg[i] >> 4
            th = seg[i] & 0x0F
            i += 1
            counts = list(seg[i:i + 16])
            i += 16
            total = sum(counts)
            symbols = list(seg[i:i + total])
            i += total
            h = _Huff(counts, symbols)
            if tc == 0:
                self.ht_dc[th] = h
            else:
                self.ht_ac[th] = h

    def _parse_sof(self, seg: bytes) -> None:
        if len(seg) < 6:
            raise JPEGError("SOF0 段太短")
        self.precision = seg[0]
        self.height, self.width = struct.unpack(">HH", seg[1:5])
        nc = seg[5]
        if self.precision != 8:
            raise JPEGError("只支持 8 位精度，收到 %d" % self.precision)
        comps = []
        for k in range(nc):
            base = 6 + k * 3
            if base + 2 >= len(seg):
                break
            cid = seg[base]
            hv = seg[base + 1]
            tq = seg[base + 2]
            comps.append({
                "id": cid,
                "h": hv >> 4,
                "v": hv & 0x0F,
                "tq": tq,
                "pred": 0,
                "data": None,
            })
        self.comps = comps

    def _parse_sos(self, seg: bytes) -> None:
        ns = seg[0]
        out = []
        for k in range(ns):
            base = 1 + k * 2
            if base + 1 >= len(seg):
                break
            cs = seg[base]
            td = seg[base + 1] >> 4
            ta = seg[base + 1] & 0x0F
            out.append({"id": cs, "td": td, "ta": ta})
        self.sos_comps = out

    # ------------------------------------------------------------ 解码
    def decode(self) -> Tuple[int, int, bytes, int]:
        """返回 (width, height, rgb 字节, 3)。"""
        if not self.comps:
            raise JPEGError("没有帧信息")
        if not self.sos_comps:
            raise JPEGError("没有扫描信息")

        hmax = max(c["h"] for c in self.comps) or 1
        vmax = max(c["v"] for c in self.comps) or 1
        mcux = (self.width + 8 * hmax - 1) // (8 * hmax)
        mcuy = (self.height + 8 * vmax - 1) // (8 * vmax)

        # 每个分量的平面（按块网格分配）
        for c in self.comps:
            c["bw"] = mcux * c["h"]        # 块列数
            c["bh"] = mcuy * c["v"]        # 块行数
            c["data"] = bytearray(c["bw"] * 8 * c["bh"] * 8)
            c["pred"] = 0

        bmap = {c["id"]: c for c in self.comps}
        scan = [{"c": bmap[s["id"]],
                 "dc": self.ht_dc.get(s["td"]),
                 "ac": self.ht_ac.get(s["ta"])} for s in self.sos_comps]
        for s in scan:
            if s["dc"] is None or s["ac"] is None:
                raise JPEGError("扫描引用了不存在的 Huffman 表")

        br = _BitReader(self.data, self.scan_start)
        ncomp = len(scan)
        total_mcu = mcux * mcuy
        rst_left = self.restart_interval or 0

        for my in range(mcuy):
            for mx in range(mcux):
                if rst_left and (my * mcux + mx) and \
                        (my * mcux + mx) % rst_left == 0:
                    br.align()
                    # 跳过 RSTn 标记
                    p = br.pos
                    while p < len(self.data) - 1:
                        if self.data[p] == 0xFF and 0xD0 <= self.data[p + 1] <= 0xD7:
                            p += 2
                            break
                        p += 1
                    br.pos = p
                    br.bits = 0
                    for s in scan:
                        s["c"]["pred"] = 0
                for s in scan:
                    c = s["c"]
                    for by in range(c["v"]):
                        for bx in range(c["h"]):
                            block = self._decode_block(br, s, c)
                            self._store_block(c, mx * c["h"] + bx,
                                              my * c["v"] + by, block)

        return self.width, self.height, self._to_rgb(), 3

    def _decode_block(self, br: _BitReader, s: dict, c: dict) -> List[int]:
        coef = [0] * 64
        qt = self.qt.get(c["tq"]) or [1] * 64
        # DC：差分编码
        nbits = s["dc"].decode(br)
        if nbits < 0:
            raise JPEGError("DC Huffman 解码失败")
        diff = _extend(br.read_bits(nbits), nbits) if nbits else 0
        c["pred"] += diff
        coef[0] = c["pred"] * qt[0]
        # AC：run-length
        k = 1
        nonzero = False
        while k < 64:
            rs = s["ac"].decode(br)
            if rs < 0:
                break
            r = rs >> 4
            sz = rs & 0x0F
            if sz == 0:
                if r == 15:
                    k += 16          # ZRL
                    continue
                break                # EOB
            k += r
            if k > 63:
                break
            val = _extend(br.read_bits(sz), sz)
            coef[ZIGZAG[k]] = val * qt[k]
            if val:
                nonzero = True
            k += 1
        if not nonzero:
            # 只有 DC 的块：IDCT 退化成「DC/8 + 128」的常数填充。
            # 图表、纯色背景这类图片里绝大多数块都是这种，跳过 IDCT 省一大截。
            v = int(coef[0] / 8.0 + 128.5)
            v = 0 if v < 0 else (255 if v > 255 else v)
            return [v] * 64
        # 反量化已经做过（coef 里是乘过量化表的自然序），Rust 侧只做 IDCT，
        # 所以量化表传全 1，避免乘两次。
        fast = _fast.idct8x8(coef, [1] * 64)
        if fast is not None:
            return list(fast)
        return _idct8x8(coef)

    def _store_block(self, c: dict, bx: int, by: int, block: List[int]) -> None:
        bw = c["bw"]
        w = bw * 8
        base = by * 8 * w + bx * 8
        data = c["data"]
        for y in range(8):
            src = y * 8
            dst = base + y * w
            data[dst:dst + 8] = block[src:src + 8]

    def _to_rgb(self) -> bytes:
        W, H = self.width, self.height
        if len(self.comps) == 3:
            ys, cb, cr = self.comps[0], self.comps[1], self.comps[2]
            hmax = max(c["h"] for c in self.comps) or 1
            vmax = max(c["v"] for c in self.comps) or 1
            got = _fast.ycbcr_to_rgb(
                ys["data"], cb["data"], cr["data"],
                W, H,
                ys["bw"] * 8, ys["bh"] * 8,      # Y 平面真实行宽/行数
                cb["bw"] * 8, cb["bh"] * 8,      # 色度平面行宽/行数
                max(1, hmax // (cb["h"] or 1)),
                max(1, vmax // (cb["v"] or 1)))
            if got is not None:
                return got
        if len(self.comps) == 1:
            fast = _fast.gray_to_rgb(self.comps[0]["data"], W * H)
            if fast is not None:
                return fast
        out = bytearray(W * H * 3)
        if len(self.comps) == 1:
            c = self.comps[0]
            d = c["data"]
            w = c["bw"] * 8
            for y in range(H):
                row = y * w
                o = y * W * 3
                for x in range(W):
                    v = d[row + x]
                    o3 = o + x * 3
                    out[o3] = out[o3 + 1] = out[o3 + 2] = v
            return bytes(out)

        hmax = max(c["h"] for c in self.comps) or 1
        vmax = max(c["v"] for c in self.comps) or 1
        ys, cb, cr = self.comps[0], self.comps[1], self.comps[2]
        yw = ys["bw"] * 8
        cw = cb["bw"] * 8
        ch = cb["bh"] * 8
        yd, cd, rd = ys["data"], cb["data"], cr["data"]
        sh = hmax // (cb["h"] or 1) or 1
        sv = vmax // (cb["v"] or 1) or 1
        for y in range(H):
            yrow = y * yw
            cy = min(ch - 1, y // sv)
            crow = cy * cw
            o = y * W * 3
            for x in range(W):
                Y = yd[yrow + x]
                cx = min(cw - 1, x // sh)
                Cb = cd[crow + cx] - 128
                Cr = rd[crow + cx] - 128
                o3 = o + x * 3
                r = Y + 1.402 * Cr
                g = Y - 0.344136 * Cb - 0.714136 * Cr
                b = Y + 1.772 * Cb
                out[o3] = 0 if r < 0 else (255 if r > 255 else int(r + 0.5))
                out[o3 + 1] = 0 if g < 0 else (255 if g > 255 else int(g + 0.5))
                out[o3 + 2] = 0 if b < 0 else (255 if b > 255 else int(b + 0.5))
        return bytes(out)


def decode(data: bytes) -> Tuple[int, int, bytes, int]:
    """解一张 JPEG，返回 (width, height, rgb, 3)。"""
    return JPEG(data).decode()

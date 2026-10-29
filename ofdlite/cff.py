# -*- coding: utf-8 -*-
"""CFF（Compact Font Format）解析与 Type2 charstring 执行。

为什么必须有：GB/T 33190-2016 表 44 明文规定
    FontFile —— 指向内嵌字型文件，嵌入字型文件应使用 OpenType 格式。
OpenType 容器里有两类轮廓：TrueType 的 glyf 表与 CFF 的 Type2 charstring。
之前只做了 glyf，遇到 .otf（思源、部分 Noto CJK）就只能告警退到兜底字体，
这在标准口径下是不完整的。

覆盖范围（够渲染用）：
  - CFF 头、INDEX（Name / TopDICT / String / GSubr / CharStrings）
  - DICT 操作数（整数、实数、运算符）与常用键
  - Private DICT、Local Subr
  - Type2 charstring 的路径类指令：
      rmoveto / hmoveto / vmoveto
      rlineto / hlineto / vlineto
      rrcurveto / hhcurveto / hvcurveto / vhcurveto / vvcurveto
      rcurveline / rlinecurve / vvcurveto / hhcurveto
      flex / hflex / flex1 / hflex1（按多段三次贝塞尔处理）
      callsubr / callgsubr / return / endchar
      hintmask / cntrmask（跳过 hint 数据字节）
      hstem / vstem / hstemhm / vstemhm（只占操作数，不影响轮廓）

不做的：CFF2（可变字体）、seac 组合字符、hint 的实际作用（栅格化压平轮廓时不需要）。
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

Point = Tuple[float, float]


class CFFError(Exception):
    pass


# ---------------------------------------------------------------- 基础读取

def read_index(data: bytes, pos: int) -> Tuple[List[bytes], int]:
    """读一个 CFF INDEX，返回 (条目字节列表, 下一个位置)。"""
    if pos + 2 > len(data):
        raise CFFError("INDEX 越界")
    count = struct.unpack(">H", data[pos:pos + 2])[0]
    if count == 0:
        return [], pos + 2
    off_size = data[pos + 2]
    if off_size < 1 or off_size > 4:
        raise CFFError("INDEX offSize 非法: %d" % off_size)
    base = pos + 3
    offs = []
    for i in range(count + 1):
        p = base + i * off_size
        if p + off_size > len(data):
            raise CFFError("INDEX 偏移越界")
        v = int.from_bytes(data[p:p + off_size], "big")
        offs.append(v)
    data_base = base + (count + 1) * off_size - 1   # 偏移是 1-based
    items = []
    for i in range(count):
        a = data_base + offs[i]
        b = data_base + offs[i + 1]
        items.append(data[a:b])
    return items, data_base + offs[-1]


# DICT 的两个特殊字节：实数与运算符
def parse_dict(raw: bytes) -> Dict[int, List[float]]:
    """解析 CFF DICT，返回 {operator: [操作数...]}。"""
    out: Dict[int, List[float]] = {}
    stack: List[float] = []
    i = 0
    n = len(raw)
    while i < n:
        b0 = raw[i]
        if b0 <= 21:                       # 运算符
            if b0 == 12:
                if i + 1 >= n:
                    break
                op = 1200 + raw[i + 1]
                i += 2
            else:
                op = b0
                i += 1
            out[op] = stack
            stack = []
        elif b0 == 28:                     # 16 位整数
            if i + 3 > n:
                break
            stack.append(float(struct.unpack(">h", raw[i + 1:i + 3])[0]))
            i += 3
        elif b0 == 29:                     # 32 位整数
            if i + 5 > n:
                break
            stack.append(float(struct.unpack(">i", raw[i + 1:i + 5])[0]))
            i += 5
        elif b0 == 30:                     # 实数
            i += 1
            s = ""
            done = False
            while i < n and not done:
                byte = raw[i]
                i += 1
                for nib in (byte >> 4, byte & 0x0F):
                    if nib == 0x0F:
                        done = True
                        break
                    if nib == 0x0A:
                        s += "."
                    elif nib == 0x0B:
                        s += "E"
                    elif nib == 0x0C:
                        s += "E-"
                    elif nib == 0x0E:
                        s += "-"
                    elif nib <= 9:
                        s += str(nib)
            try:
                stack.append(float(s) if s else 0.0)
            except ValueError:
                stack.append(0.0)
        elif 32 <= b0 <= 246:              # -107..107
            stack.append(float(b0 - 139))
            i += 1
        elif 247 <= b0 <= 250:
            if i + 2 > n:
                break
            stack.append(float((b0 - 247) * 256 + raw[i + 1] + 108))
            i += 2
        elif 251 <= b0 <= 254:
            if i + 2 > n:
                break
            stack.append(float(-(b0 - 251) * 256 - raw[i + 1] - 108))
            i += 2
        else:
            i += 1
    return out


# ---------------------------------------------------------------- CFF 字体

class CFF(object):
    """一个 CFF 表。坐标单位是 charstring 空间（通常 1000 或 2048）。"""

    def __init__(self, data: bytes, base: int = 0):
        self.data = data
        self.base = base
        p = base
        if p + 4 > len(data):
            raise CFFError("CFF 头不完整")
        major = data[p]
        hdr_size = data[p + 2]
        if major != 1:
            raise CFFError("只支持 CFF 1.x（收到 major=%d）" % major)
        p += hdr_size

        self.names, p = read_index(data, p)
        self.top_dicts_raw, p = read_index(data, p)
        self.strings, p = read_index(data, p)
        self.gsubrs, p = read_index(data, p)

        if not self.top_dicts_raw:
            raise CFFError("CFF 里没有 TopDICT")
        self.top = parse_dict(self.top_dicts_raw[0])

        self.charstrings: List[bytes] = []
        cs_off = self.top.get(17, [0])[0]
        if cs_off:
            self.charstrings, _ = read_index(data, base + int(cs_off))

        # FontMatrix（默认 0.001）
        fm = self.top.get(1207, [0.001, 0.0, 0.0, 0.001, 0.0, 0.0])
        self.font_matrix = fm if len(fm) >= 6 else [0.001, 0, 0, 0.001, 0, 0]
        # CharstringType 只有 2 有意义
        self.charstring_type = int(self.top.get(1206, [2])[0])

        # Private DICT 与 Local Subr
        self.local_subrs: List[bytes] = []
        self.default_width = 0.0
        self.nominal_width = 0.0
        priv = self.top.get(18)
        if priv and len(priv) >= 2:
            psize, poff = int(priv[0]), int(priv[1])
            praw = data[base + poff:base + poff + psize]
            pdict = parse_dict(praw)
            if 20 in pdict:
                self.default_width = pdict[20][0]
            if 21 in pdict:
                self.nominal_width = pdict[21][0]
            sub_off = pdict.get(19)
            if sub_off:
                try:
                    self.local_subrs, _ = read_index(data, base + poff + int(sub_off[0]))
                except CFFError:
                    self.local_subrs = []

    # ------------------------------------------------------------ 查询
    def glyph_count(self) -> int:
        return len(self.charstrings)

    def glyph_outlines(self, gid: int) -> List[List[Point]]:
        """执行该字形的 Type2 charstring，返回轮廓折线（charstring 单位）。"""
        if not (0 <= gid < len(self.charstrings)):
            return []
        ex = _T2Exec(self.charstrings[gid], self.gsubrs, self.local_subrs,
                     self.default_width)
        try:
            return ex.run()
        except (IndexError, struct.error):
            # 指令流坏了就返回已画出的部分，不让整个渲染崩掉
            return ex.contours


# ---------------------------------------------------------------- Type2 执行

class _T2Exec(object):
    """Type2 charstring 解释器：只求路径，不求 hint。"""

    def __init__(self, code: bytes, gsubrs: List[bytes], lsubrs: List[bytes],
                 default_width: float = 0.0):
        self.code = code
        self.gsubrs = gsubrs
        self.lsubrs = lsubrs
        self.stack: List[float] = []
        self.contours: List[List[Point]] = []
        self.cur: List[Point] = []
        self.x = 0.0
        self.y = 0.0
        self.done = False
        self.nstems = 0
        self.width_seen = False
        self.default_width = default_width
        self.depth = 0

    # -------- 路径累积
    def _moveto(self, dx: float, dy: float) -> None:
        self._flush()
        self.x += dx
        self.y += dy
        self.cur = [(self.x, self.y)]

    def _lineto(self, dx: float, dy: float) -> None:
        self.x += dx
        self.y += dy
        if not self.cur:
            self.cur = [(self.x, self.y)]
        else:
            self.cur.append((self.x, self.y))

    def _curveto(self, dx1, dy1, dx2, dy2, dx3, dy3) -> None:
        x0, y0 = self.x, self.y
        x1, y1 = x0 + dx1, y0 + dy1
        x2, y2 = x1 + dx2, y1 + dy2
        x3, y3 = x2 + dx3, y2 + dy3
        if not self.cur:
            self.cur = [(x0, y0)]
        for i in range(1, 9):
            t = i / 8.0
            mt = 1.0 - t
            self.cur.append((
                mt * mt * mt * x0 + 3 * mt * mt * t * x1 + 3 * mt * t * t * x2 + t * t * t * x3,
                mt * mt * mt * y0 + 3 * mt * mt * t * y1 + 3 * mt * t * t * y2 + t * t * t * y3,
            ))
        self.x, self.y = x3, y3

    def _flush(self) -> None:
        if len(self.cur) >= 3:
            c = self.cur
            if c[0] != c[-1]:
                c = c + [c[0]]
            self.contours.append(c)
        self.cur = []

    # -------- 执行
    def run(self) -> List[List[Point]]:
        self._exec(self.code, 0)
        self._flush()
        return self.contours

    def _exec(self, code: bytes, level: int) -> None:
        if level > 10:
            return
        i = 0
        n = len(code)
        st = self.stack
        while i < n:
            b0 = code[i]

            # ---- 操作数
            if b0 == 28:
                st.append(float(struct.unpack(">h", code[i + 1:i + 3])[0]))
                i += 3
                continue
            if 32 <= b0 <= 246:
                st.append(float(b0 - 139))
                i += 1
                continue
            if 247 <= b0 <= 250:
                st.append(float((b0 - 247) * 256 + code[i + 1] + 108))
                i += 2
                continue
            if 251 <= b0 <= 254:
                st.append(float(-(b0 - 251) * 256 - code[i + 1] - 108))
                i += 2
                continue
            if b0 == 255:                     # 16.16 定点
                st.append(struct.unpack(">i", code[i + 1:i + 5])[0] / 65536.0)
                i += 5
                continue

            i += 1
            # ---- 运算符
            if b0 in (1, 3, 18, 23):          # hstem / vstem / hstemhm / vstemhm
                self._stem()
                continue
            if b0 == 19 or b0 == 20:          # hintmask / cntrmask
                self._stem()
                # 跳过后面的 hint 字节：每 8 个 stem 一个字节
                i += (self.nstems + 7) // 8
                continue
            if b0 == 21:                      # rmoveto
                self._drop_width()
                dx = st[-2] if len(st) >= 2 else 0.0
                dy = st[-1] if len(st) >= 2 else 0.0
                st[:] = []
                self._moveto(dx, dy)
                continue
            if b0 == 22:                      # hmoveto
                self._drop_width()
                dx = st[-1] if st else 0.0
                st[:] = []
                self._moveto(dx, 0.0)
                continue
            if b0 == 4:                       # vmoveto
                self._drop_width()
                dy = st[-1] if st else 0.0
                st[:] = []
                self._moveto(0.0, dy)
                continue
            if b0 == 5:                       # rlineto
                for k in range(0, len(st) - 1, 2):
                    self._lineto(st[k], st[k + 1])
                st[:] = []
                continue
            if b0 == 6:                       # hlineto（交替水平/垂直）
                horiz = True
                for v in st:
                    if horiz:
                        self._lineto(v, 0.0)
                    else:
                        self._lineto(0.0, v)
                    horiz = not horiz
                st[:] = []
                continue
            if b0 == 7:                       # vlineto
                horiz = False
                for v in st:
                    if horiz:
                        self._lineto(v, 0.0)
                    else:
                        self._lineto(0.0, v)
                    horiz = not horiz
                st[:] = []
                continue
            if b0 == 8:                       # rrcurveto
                for k in range(0, len(st) - 5, 6):
                    self._curveto(st[k], st[k + 1], st[k + 2],
                                  st[k + 3], st[k + 4], st[k + 5])
                st[:] = []
                continue
            if b0 == 24:                      # rcurveline
                k = 0
                while k + 5 < len(st) - 1:
                    self._curveto(st[k], st[k + 1], st[k + 2],
                                  st[k + 3], st[k + 4], st[k + 5])
                    k += 6
                if k + 1 < len(st):
                    self._lineto(st[k], st[k + 1])
                st[:] = []
                continue
            if b0 == 25:                      # rlinecurve
                k = 0
                while k + 1 < len(st) - 6:
                    self._lineto(st[k], st[k + 1])
                    k += 2
                if k + 5 < len(st):
                    self._curveto(st[k], st[k + 1], st[k + 2],
                                  st[k + 3], st[k + 4], st[k + 5])
                st[:] = []
                continue
            if b0 == 26 or b0 == 27:          # vvcurveto / hhcurveto
                k = 0
                first = 0.0
                if len(st) % 4 == 1:
                    first = st[0]
                    k = 1
                while k + 3 < len(st):
                    if b0 == 26:              # vvcurveto: dx1? dy1 dx2 dy2 dy3
                        d1 = first if first else 0.0
                        self._curveto(d1, st[k], st[k + 1], st[k + 2],
                                      0.0, st[k + 3])
                        first = 0.0
                    else:                     # hhcurveto: dy1? dx1 dx2 dy2 dx3
                        d1 = first if first else 0.0
                        self._curveto(st[k], d1, st[k + 1], st[k + 2],
                                      st[k + 3], 0.0)
                        first = 0.0
                    k += 4
                st[:] = []
                continue
            if b0 == 30 or b0 == 31:          # vhcurveto / hvcurveto
                horiz = (b0 == 31)
                k = 0
                while k + 3 < len(st):
                    if horiz:
                        self._curveto(st[k], 0.0, st[k + 1], st[k + 2],
                                      0.0, st[k + 3])
                    else:
                        self._curveto(0.0, st[k], st[k + 1], st[k + 2],
                                      st[k + 3], 0.0)
                    horiz = not horiz
                    k += 4
                st[:] = []
                continue
            if b0 == 10:                      # callsubr
                idx = int(st.pop()) if st else 0
                idx += self.bias(self.lsubrs)
                if 0 <= idx < len(self.lsubrs):
                    self._exec(self.lsubrs[idx], level + 1)
                continue
            if b0 == 29:                      # callgsubr
                idx = int(st.pop()) if st else 0
                idx += self.bias(self.gsubrs)
                if 0 <= idx < len(self.gsubrs):
                    self._exec(self.gsubrs[idx], level + 1)
                continue
            if b0 == 11:                      # return
                return
            if b0 == 14:                      # endchar
                self._flush()
                self.done = True
                return
            if b0 == 12:                      # 转义
                if i >= n:
                    return
                b1 = code[i]
                i += 1
                if b1 == 35:                  # flex
                    if len(st) >= 12:
                        self._curveto(st[0], st[1], st[2], st[3], st[4], st[5])
                        self._curveto(st[6], st[7], st[8], st[9], st[10], st[11])
                    st[:] = []
                elif b1 == 34:                # hflex
                    if len(st) >= 7:
                        y0 = self.y
                        self._curveto(st[0], 0.0, st[1], st[2], st[3], 0.0)
                        self._curveto(st[4], 0.0, st[5], y0 - self.y, st[6], 0.0)
                    st[:] = []
                elif b1 == 36:                # hflex1
                    if len(st) >= 9:
                        y0 = self.y
                        self._curveto(st[0], st[1], st[2], st[3], st[4], 0.0)
                        self._curveto(st[5], 0.0, st[6], st[7], st[8], y0 - self.y)
                    st[:] = []
                elif b1 == 37:                # flex1
                    if len(st) >= 11:
                        sx, sy = self.x, self.y
                        self._curveto(st[0], st[1], st[2], st[3], st[4], st[5])
                        self._curveto(st[6], st[7], st[8], st[9], st[10], 0.0)
                        # 最后一段的 dy 由起点到终点决定
                        dx = st[0] + st[2] + st[4] + st[6] + st[8] + st[10]
                        dy = st[1] + st[3] + st[5] + st[7] + st[9]
                        if abs(dx) > abs(dy):
                            self.x = sx + dx
                            self.y = sy + dy
                    st[:] = []
                else:
                    # 算术/逻辑运算符：遇到就当无操作，清栈避免污染
                    if b1 in (3, 4, 5, 9, 10, 11, 12, 14, 15, 18, 24, 26,
                              27, 28, 29, 30):
                        pass
                    st[:] = []
                continue
            # 其它未知字节：跳过（清栈防止错位）
            st[:] = []

    def _stem(self) -> None:
        """hstem/vstem 系列：只数 hint 个数，操作数丢弃。"""
        self._drop_width()
        self.nstems += len(self.stack) // 2
        self.stack[:] = []

    def _drop_width(self) -> None:
        """charstring 开头可能多一个 width 操作数（奇数个栈元素）。"""
        if self.width_seen:
            return
        self.width_seen = True
        if len(self.stack) % 2 == 1:
            self.stack.pop(0)

    @staticmethod
    def bias(subrs: List[bytes]) -> int:
        n = len(subrs)
        if n < 1240:
            return 107
        if n < 33900:
            return 1131
        return 32768

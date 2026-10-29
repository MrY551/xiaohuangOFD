# -*- coding: utf-8 -*-
"""自己写的 TrueType 字体解析与字形轮廓提取（不依赖 freetype/fontTools）。

为什么要自己写：项目原则是「能自己写的对照标准自己写实现」。
用 freetype 就把整个字形解析交给别人了，而 OFD 需要的只是
「字符 → 轮廓折线」这一步，规范清楚（OpenType/TrueType 的 glyf 表），
自己写完全可控，也不必给麒麟端再加一个 .so 依赖。

覆盖的表（够渲染用）：
  head   unitsPerEm、indexToLocFormat
  maxp   numGlyphs
  hhea   numberOfHMetrics
  hmtx   每个字形的 advanceWidth
  cmap   format 4（BMP，最常见）与 format 12（全 Unicode）
  loca   字形数据偏移
  glyf   简单字形 + 复合字形的轮廓

不做的：hinting（栅格化时直接压平轮廓）、变体（GSUB/GPOS）、
CFF 轮廓（OTF）。缺这些只影响字形微调，不影响「画出字」。
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

Point = Tuple[float, float]


class FontError(Exception):
    pass


class Font(object):
    """一个 TrueType 字体。坐标单位是 font units（见 units_per_em）。"""

    def __init__(self, data: bytes):
        self.data = data
        self.tables: Dict[str, Tuple[int, int]] = {}
        self.units_per_em = 1000
        self.num_glyphs = 0
        self.num_hmetrics = 0
        self.index_to_loc = 0
        self.cmap: Dict[int, int] = {}
        self._loca: List[int] = []
        self._adv: List[int] = []
        self._glyph_cache: Dict[int, List[List[Point]]] = {}
        # CFF（OpenType 的另一种轮廓形式）：标准表44 要求内嵌字体用 OpenType，
        # 而 OpenType 可能是 glyf 也可能是 CFF，两者都得支持。
        self._cff = None
        self._parse_directory()
        self._parse_head()
        self._parse_maxp()
        if "CFF " in self.tables:
            from . import cff as _cff
            self._cff = _cff.CFF(self.data, self.tables["CFF "][0])
        self._parse_hhea()
        self._parse_hmtx()
        self._parse_cmap()
        if self._cff is None:
            self._parse_loca()
        else:
            # CFF 字体没有 loca/glyf，字形数也能从 charstrings 得到
            n = self._cff.glyph_count()
            if n > 0:
                self.num_glyphs = n

    # ------------------------------------------------------------ 基础
    def _u16(self, off: int) -> int:
        return struct.unpack(">H", self.data[off:off + 2])[0]

    def _s16(self, off: int) -> int:
        return struct.unpack(">h", self.data[off:off + 2])[0]

    def _u32(self, off: int) -> int:
        return struct.unpack(">I", self.data[off:off + 4])[0]

    def _table(self, name: str) -> Optional[Tuple[int, int]]:
        return self.tables.get(name)

    def _parse_directory(self) -> None:
        if len(self.data) < 12:
            raise FontError("文件太短")
        tag = self.data[:4]
        base = 0
        if tag == b"ttcf":
            # 字体集合：取第一个子字体的表目录（Linux 上中文字体常是 .ttc）
            if len(self.data) < 16:
                raise FontError("TTC 头不完整")
            base = self._u32(12)
            tag = self.data[base:base + 4]
        if tag not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
            raise FontError("不是 TrueType 字体（tag=%r）" % tag)
        num = self._u16(base + 4)
        for i in range(num):
            off = base + 12 + i * 16
            name = self.data[off:off + 4].decode("latin-1")
            t_off = self._u32(off + 8)
            t_len = self._u32(off + 12)
            self.tables[name] = (t_off, t_len)

    def _parse_head(self) -> None:
        t = self._table("head")
        if not t:
            raise FontError("缺 head 表")
        base = t[0]
        self.units_per_em = self._u16(base + 18) or 1000
        self.index_to_loc = self._s16(base + 50)

    def _parse_maxp(self) -> None:
        t = self._table("maxp")
        if not t:
            raise FontError("缺 maxp 表")
        self.num_glyphs = self._u16(t[0] + 4)

    def _parse_hhea(self) -> None:
        t = self._table("hhea")
        if not t:
            raise FontError("缺 hhea 表")
        self.num_hmetrics = self._u16(t[0] + 34)

    def _parse_hmtx(self) -> None:
        t = self._table("hmtx")
        if not t:
            return
        base = t[0]
        adv = []
        for i in range(self.num_hmetrics):
            adv.append(self._u16(base + i * 4))
        # 后面字形沿用最后一个 advance
        last = adv[-1] if adv else self.units_per_em
        for _ in range(self.num_glyphs - len(adv)):
            adv.append(last)
        self._adv = adv

    def _parse_cmap(self) -> None:
        t = self._table("cmap")
        if not t:
            # 内嵌子集字体常没有 cmap —— 标准 11.4 的 CGTransform 直接给字形索引，
            # 本来就不需要 cmap。这里只置空，不报错。
            self.cmap = {}
            return
        base = t[0]
        n = self._u16(base + 2)
        best = None
        for i in range(n):
            rec = base + 4 + i * 8
            pid = self._u16(rec)
            eid = self._u16(rec + 2)
            off = self._u32(rec + 4)
            sub = base + off
            fmt = self._u16(sub)
            # 优先 3/10（全 Unicode）、其次 3/1（BMP）、再 0/x
            score = 0
            if (pid, eid) == (3, 10):
                score = 3
            elif (pid, eid) == (3, 1):
                score = 2
            elif pid == 0:
                score = 1
            if fmt in (4, 12) and (best is None or score > best[0]):
                best = (score, sub, fmt)
        if not best:
            raise FontError("cmap 里没有 format 4 / 12 子表")
        _score, sub, fmt = best
        if fmt == 4:
            self._parse_cmap4(sub)
        else:
            self._parse_cmap12(sub)

    def _parse_cmap4(self, sub: int) -> None:
        seg_x2 = self._u16(sub + 6)
        seg = seg_x2 // 2
        end_base = sub + 14
        start_base = end_base + seg_x2 + 2
        delta_base = start_base + seg_x2
        range_base = delta_base + seg_x2
        for i in range(seg):
            end = self._u16(end_base + i * 2)
            start = self._u16(start_base + i * 2)
            delta = self._s16(delta_base + i * 2)
            ro = self._u16(range_base + i * 2)
            if start == 0xFFFF:
                continue
            for ch in range(start, min(end, 0xFFFE) + 1):
                if ro == 0:
                    gid = (ch + delta) & 0xFFFF
                else:
                    gi = range_base + i * 2 + ro + (ch - start) * 2
                    if gi + 2 > len(self.data):
                        continue
                    gid = self._u16(gi)
                    if gid:
                        gid = (gid + delta) & 0xFFFF
                if gid:
                    self.cmap[ch] = gid

    def _parse_cmap12(self, sub: int) -> None:
        n = self._u32(sub + 12)
        for i in range(n):
            rec = sub + 16 + i * 12
            start = self._u32(rec)
            end = self._u32(rec + 4)
            gid0 = self._u32(rec + 8)
            span = min(end, 0x10FFFF) - start
            if span > 0x20000:      # 防御：超大区间不逐码展开
                continue
            for k in range(span + 1):
                self.cmap[start + k] = gid0 + k

    def _parse_loca(self) -> None:
        t = self._table("loca")
        if not t:
            raise FontError("缺 loca 表")
        base = t[0]
        loca = []
        if self.index_to_loc == 0:
            for i in range(self.num_glyphs + 1):
                loca.append(self._u16(base + i * 2) * 2)
        else:
            for i in range(self.num_glyphs + 1):
                loca.append(self._u32(base + i * 4))
        self._loca = loca

    # ------------------------------------------------------------ 查询
    def glyph_index(self, ch: str) -> int:
        if not ch:
            return 0
        return self.cmap.get(ord(ch[0]), 0)

    def advance(self, gid: int) -> int:
        if 0 <= gid < len(self._adv):
            return self._adv[gid]
        return self.units_per_em // 2

    # ------------------------------------------------------------ 轮廓
    def glyph_outlines(self, gid: int, _depth: int = 0) -> List[List[Point]]:
        """返回该字形的轮廓（若干闭合折线，font units）。

        复合字形会递归展开子字形（带 2x2 变换与偏移）。
        """
        if gid in self._glyph_cache:
            return self._glyph_cache[gid]
        out: List[List[Point]] = []
        if not (0 <= gid < self.num_glyphs) or _depth > 6:
            return out
        if self._cff is not None:
            # CFF：执行 Type2 charstring 得到轮廓。
            # charstring 坐标要按 FontMatrix 换到 font units：
            # FontMatrix[0] 是「1 单位等于多少 em」，units_per_em 是 em 的单位数。
            scale = 1.0
            fm = self._cff.font_matrix
            if fm and fm[0] > 0:
                scale = 1.0 / (fm[0] * float(self.units_per_em or 1000))
            raw = self._cff.glyph_outlines(gid)
            if abs(scale - 1.0) > 1e-9:
                raw = [[(x * scale, y * scale) for x, y in c] for c in raw]
            self._glyph_cache[gid] = raw
            return raw
        glyf = self._table("glyf")
        if not glyf:
            return out
        start = self._loca[gid]
        end = self._loca[gid + 1]
        if end <= start:
            self._glyph_cache[gid] = out
            return out
        base = glyf[0] + start
        ncont = self._s16(base)
        if ncont >= 0:
            out = self._simple_glyph(base, ncont)
        else:
            out = self._composite_glyph(base, _depth)
        self._glyph_cache[gid] = out
        return out

    def _simple_glyph(self, base: int, ncont: int) -> List[List[Point]]:
        if ncont == 0:
            return []
        end_pts = []
        p = base + 10
        for _ in range(ncont):
            end_pts.append(self._u16(p))
            p += 2
        npts = end_pts[-1] + 1
        ins_len = self._u16(p)
        p += 2 + ins_len
        flags = []
        while len(flags) < npts:
            f = self.data[p]
            p += 1
            flags.append(f)
            if f & 8:                       # repeat
                rep = self.data[p]
                p += 1
                flags.extend([f] * rep)
        flags = flags[:npts]
        xs = []
        v = 0
        for f in flags:
            if f & 2:                       # x 短向量
                d = self.data[p]
                p += 1
                v += d if (f & 16) else -d
            elif not (f & 16):              # 不走 dx=0 分支
                v += self._s16(p)
                p += 2
            xs.append(v)
        ys = []
        v = 0
        for f in flags:
            if f & 4:
                d = self.data[p]
                p += 1
                v += d if (f & 32) else -d
            elif not (f & 32):
                v += self._s16(p)
                p += 2
            ys.append(v)

        pts = list(zip(xs, ys))
        on = [bool(f & 1) for f in flags]
        contours = []
        s = 0
        for e in end_pts:
            seg = list(range(s, e + 1))
            if seg:
                contours.append(self._fit_contour(pts, on, seg))
            s = e + 1
        return [c for c in contours if len(c) >= 3]

    def _composite_glyph(self, base: int, depth: int) -> List[List[Point]]:
        out: List[List[Point]] = []
        p = base + 10
        while True:
            flags = self._u16(p)
            gid = self._u16(p + 2)
            p += 4
            if flags & 1:                   # ARG_1_AND_2_ARE_WORDS
                dx = self._s16(p)
                dy = self._s16(p + 2)
                p += 4
            else:
                dx = struct.unpack(">b", self.data[p:p + 1])[0]
                dy = struct.unpack(">b", self.data[p + 1:p + 2])[0]
                p += 2
            a = d = 1.0
            b = c = 0.0
            if flags & 8:                   # WE_HAVE_A_SCALE
                a = d = self._s16(p) / 16384.0
                p += 2
            elif flags & 0x40:              # X_AND_Y_SCALE
                a = self._s16(p) / 16384.0
                d = self._s16(p + 2) / 16384.0
                p += 4
            elif flags & 0x80:              # TWO_BY_TWO
                a = self._s16(p) / 16384.0
                b = self._s16(p + 2) / 16384.0
                c = self._s16(p + 4) / 16384.0
                d = self._s16(p + 6) / 16384.0
                p += 8
            for contour in self.glyph_outlines(gid, depth + 1):
                out.append([(x * a + y * c + dx, x * b + y * d + dy)
                            for x, y in contour])
            if not (flags & 0x20):          # MORE_COMPONENTS
                break
        return out

    # ------------------------------------------------------------ 曲线
    @staticmethod
    def _fit_contour(pts: List[Point], on: List[bool],
                     idx: List[int]) -> List[Point]:
        """把 TrueType 的二次贝塞尔轮廓压平成折线。

        TrueType 的规则：两个相邻 off-curve 点之间隐含一个 on-curve 中点；
        首尾都是 off-curve 时，起点取末点与首点的中点。
        """
        n = len(idx)
        if n == 0:
            return []
        items = [(pts[i], on[i]) for i in idx]
        # 找起点：优先 on-curve
        start_at = None
        for k in range(n):
            if items[k][1]:
                start_at = k
                break
        if start_at is None:
            # 全是 off-curve：用首尾中点当起点
            p0 = items[-1][0]
            p1 = items[0][0]
            first = ((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0)
            seq = [(first, True)] + items + [(first, True)]
        else:
            seq = items[start_at:] + items[:start_at] + [items[start_at]]

        out: List[Point] = []
        cur = seq[0][0]
        out.append(cur)
        ctrl = None
        for k in range(1, len(seq)):
            pt, is_on = seq[k]
            if is_on:
                if ctrl is None:
                    out.append(pt)
                else:
                    out.extend(_quad_flat(cur, ctrl, pt))
                    ctrl = None
                cur = pt
            else:
                if ctrl is not None:
                    mid = ((ctrl[0] + pt[0]) / 2.0, (ctrl[1] + pt[1]) / 2.0)
                    out.extend(_quad_flat(cur, ctrl, mid))
                    cur = mid
                ctrl = pt
        if out and _dist(out[0], out[-1]) > 1e-6:
            out.append(out[0])
        return out


def _dist(a: Point, b: Point) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _quad_flat(p0: Point, p1: Point, p2: Point, steps: int = 8) -> List[Point]:
    """二次贝塞尔压平成折线（steps 段）。"""
    out = []
    for i in range(1, steps + 1):
        t = i / float(steps)
        mt = 1.0 - t
        x = mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0]
        y = mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]
        out.append((x, y))
    return out


def load(path: str) -> Font:
    with open(path, "rb") as f:
        return Font(f.read())

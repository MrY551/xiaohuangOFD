# -*- coding: utf-8 -*-
"""Pillow 渲染后端。

用途：
  * 命令行批量转 PNG/JPEG/TIFF
  * 导出 PDF（走 300dpi 位图，Pillow 原生多页 PDF）
  * 无图形界面的服务器上做缩略图/预览

限制（明确写出，不藏着）：
  * 路径填充按 nonzero 规则，EvenOdd 的镂空效果不精确
  * 不支持的图片编码（JBIG2）直接跳过并记录 warning
  * 文本用本机字体替代，度量与 WPS/数科阅读器可能有 1-2% 差异
"""
from __future__ import annotations

import base64
import io
import math
from typing import Dict, List, Optional

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import font_manager
from .errors import RenderError
from .scene import PageScene

_DEFAULT_DPI = 150
# 超采样倍数：PIL 的 ImageDraw 不做抗锯齿，直接按输出分辨率画会得到
# 硬边线条和死黑文字（官方阅读器的实心笔画是抗锯齿的灰）。
# 做法是按 dpi × ss 画，最后 LANCZOS 缩回 dpi，成本是内存 ×ss²，画质接近原版。
_DEFAULT_SUPERSAMPLE = 3


def _dash_segments(pts, pattern, offset=0.0):
    """把折线按 dash 模式切成若干实段。pattern 单位与 pts 相同（像素）。

    Pillow 的 draw.line 不支持虚线，只能自己按弧长切。

    8.2.4：`DashOffset` 指定虚线绘制偏移位置 —— 「在当前位置按绘制相反方向
    偏移 DashOffset 值指定的距离后开始绘制」。换成实现语言：把 pattern 的
    起始相位往前挪 offset，等价于从 pattern 内部某个位置起算。
    """
    if not pattern or len(pts) < 2:
        return [pts]
    pat = list(pattern)
    if len(pat) % 2:
        pat = pat * 2          # 长度为奇数时重复一遍（与 PostScript/SVG 一致）
    pat = [max(0.5, float(v)) for v in pat]
    total = sum(pat)

    # 起点相位：offset 前移，求出落在第几段、该段还剩多少
    idx, remain = 0, 0.0
    if total > 0 and offset:
        phase = offset % total
        while phase >= pat[idx]:
            phase -= pat[idx]
            idx = (idx + 1) % len(pat)
        remain = pat[idx] - phase
    else:
        remain = pat[0]

    out, cur = [], [pts[0]]
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        dx, dy = x1 - x0, y1 - y0
        seg = math.hypot(dx, dy)
        if seg <= 1e-9:
            continue
        ux, uy = dx / seg, dy / seg
        t = 0.0
        while t < seg:
            step = min(remain, seg - t)
            t += step
            px, py = x0 + ux * t, y0 + uy * t
            if idx % 2 == 0:          # 偶数下标 = 实段
                cur.append((px, py))
            remain -= step
            if remain <= 1e-9:
                if idx % 2 == 0 and len(cur) > 1:
                    out.append(cur)
                idx = (idx + 1) % len(pat)
                remain = pat[idx]
                cur = [(px, py)]
    if idx % 2 == 0 and len(cur) > 1:
        out.append(cur)
    return out


def _lerp_stops(stops, cols, t):
    """在分段色标之间线性插值，返回 RGBA（0..1）。"""
    if t <= stops[0]:
        return cols[0]
    if t >= stops[-1]:
        return cols[-1]
    for i in range(1, len(stops)):
        if t <= stops[i]:
            p0, p1 = stops[i - 1], stops[i]
            span = (p1 - p0) or 1e-9
            f = (t - p0) / span
            return tuple(cols[i - 1][ch] + (cols[i][ch] - cols[i - 1][ch]) * f
                         for ch in range(4))
    return cols[-1]


def _stroke_px(w_mm: float, dpi: float, extra: float = 1.0) -> int:
    """线宽（mm）换算成像素，按 GB/T 33190-2016 表21 的强制规定：

        当线宽大于 0 时，无论多小都最少绘制两个像素的宽度；
        当线宽为 0 时，绘制一个像素的宽度。

    这条不遵守的话，0.1mm 的细线在 150dpi 下算出来是 0.59px，取整成 1px
    会比标准要求的细一半——表格线在低 dpi 导出时会"消失一半"。
    """
    if w_mm is None or w_mm <= 0:
        return 1
    n = int(round(_px(w_mm, dpi) * extra))
    return max(2, n)



def _px(mm: float, dpi: float) -> float:
    return mm * dpi / 25.4


class PilRenderer(object):
    """把 PageScene 画成 PIL 图像。"""

    # ---------------------------------------------------------- 渐变
    def _canvas_size(self):
        return self._buf.size

    def _shading_rgba(self, shd, W, H, bbox=None):
        """把渐变算成一张 RGBA 图（整幅，供 mask 贴合）。

        纯 Pillow 实现，不依赖 numpy —— deb 的 Depends 只有 python3 + python3-pil。

        轴向：t(P) = ((P-S)·(E-S)) / |E-S|² 对 P 是**线性**的，所以整幅 t 场
        可以用一次仿射变换从水平渐变条（0..255）变换出来；再按 MapType 与
        Segments 做 256 级 LUT 映射（Repeat/Reflect 在 LUT 里处理，不必分段重画）。
        """
        # 高洛德用控制点而不是色标，所以分发要在 segments 检查之前
        if shd.get("kind") == "pattern":
            return self._pattern_rgba(shd, W, H, bbox)
        if shd.get("kind") == "lattice":
            return self._lattice_rgba(shd, W, H, bbox)
        if shd.get("kind") == "gouraud":
            return self._gouraud_rgba(shd, W, H, bbox)
        if shd.get("kind") == "radial":
            return self._radial_rgba(shd, W, H, bbox)

        segs = shd.get("segments") or []
        if not segs:
            return None

        (sx, sy), (ex, ey) = shd["start"], shd["end"]
        ax, ay = ex - sx, ey - sy
        denom = ax * ax + ay * ay
        k = 25.4 / self.work_dpi

        if denom <= 1e-12:
            return Image.new("RGBA", (W, H),
                             tuple(int(round(v * 255)) for v in segs[-1][1]))

        A = (ax * k) / denom
        B = (ay * k) / denom
        C = (-(sx * ax + sy * ay)) / denom + (ax + ay) * 0.5 * k / denom

        corners = [(0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1)]
        ts = [A * x + B * y + C for x, y in corners]
        tmin, tmax = min(ts), max(ts)
        span = (tmax - tmin) or 1.0

        a = A / span * 255.0
        b = B / span * 255.0
        c = (C - tmin) / span * 255.0

        # Image.linear_gradient 是**纵向**的，直接压成 1 行会被平均成一片 128，
        # 必须自己构造横向条
        base = Image.new("L", (256, 1))
        base.putdata(list(range(256)))
        tfield = base.transform((W, H), Image.AFFINE, (a, b, c, 0, 0, 0),
                                resample=Image.BILINEAR)

        stops = [q for q, _ in segs]
        cols = [c2 for _, c2 in segs]
        map_type = (shd.get("map") or "direct").lower()
        unit = shd.get("unit")
        lut = [[], [], [], []]
        for i in range(256):
            t = tmin + span * (i / 255.0)
            if map_type != "direct" and unit:
                L = denom ** 0.5
                tn = t * L / float(unit) if L > 1e-9 else t
            else:
                tn = t
            if map_type == "repeat":
                tn = tn - math.floor(tn)
            elif map_type == "reflect":
                fl = math.floor(tn)
                tn = tn - fl
                if int(fl) % 2:
                    tn = 1.0 - tn
            else:
                tn = min(1.0, max(0.0, tn))
            rgba = _lerp_stops(stops, cols, tn)
            for ch in range(4):
                lut[ch].append(max(0, min(255, int(round(rgba[ch] * 255)))))
        return Image.merge("RGBA", [tfield.point(lut[ch]) for ch in range(4)])

    def _radial_rgba(self, shd, W, H, bbox=None):
        """径向渐变（8.3.4.3）。

        t = 到 StartPoint 的距离 / EndRadius，再按 MapType 与 Segments 上色。

        Pillow 没有径向渐变 API，Image.radial_gradient("L") 给的是"中心 0、
        四角 255"的距离场（边缘中点只有 181）。这里按对象 bbox 造一张**覆盖整块**
        的距离场，于是 bbox 内每个点都能算 t，超出的部分由 MapType 或截断处理，
        不会像"只造 2R 方块"那样把区间外的点误当成端点色。
        """
        (sx, sy) = shd["start"]
        radii = shd.get("radii") or [0.0, 0.0]
        r_end = float(radii[1]) or 1.0
        ecc = max(0.0, min(0.99, float(shd.get("ecc") or 0.0)))
        ang = float(shd.get("angle") or 0.0)
        k = self.work_dpi / 25.4

        if bbox is None:
            bbox = (0.0, 0.0, W / k, H / k)
        # bbox 内离中心最远的距离（mm）
        maxd = 0.0
        for cx in (bbox[0], bbox[2]):
            for cy in (bbox[1], bbox[3]):
                d = ((cx - sx) ** 2 + (cy - sy) ** 2) ** 0.5
                maxd = max(maxd, d)
        maxd = max(maxd, r_end, 1e-3)

        size = max(4, int(round(maxd * 2 * k)))
        field = Image.radial_gradient("L").resize((size, size), Image.BILINEAR)
        b_ratio = (1.0 - ecc * ecc) ** 0.5 if ecc > 0 else 1.0
        if b_ratio < 0.999:
            field = field.resize((size, max(4, int(round(size * b_ratio)))),
                                 Image.BILINEAR)
        if ang:
            field = field.rotate(-ang, resample=Image.BICUBIC, expand=True)

        full = Image.new("L", (W, H), 255)
        full.paste(field, (int(round(sx * k - field.size[0] / 2.0)),
                           int(round(sy * k - field.size[1] / 2.0))))

        # radial_gradient 的 255 对应"到四角"= maxd·√2，换算回距离要乘这个系数
        # v → dist = v/255 * maxd * sqrt(2)
        stops = [q for q, _ in shd["segments"]]
        cols = [c for _, c in shd["segments"]]
        map_type = (shd.get("map") or "direct").lower()
        unit = shd.get("unit")
        dist_k = maxd * (2 ** 0.5) / 255.0

        lut = [[], [], [], []]
        for i in range(256):
            dist = i * dist_k                      # mm
            t = dist / r_end if r_end > 1e-9 else 0.0
            tn = (t * r_end / float(unit)) if (map_type != "direct" and unit) else t
            if map_type == "repeat":
                tn = tn - math.floor(tn)
            elif map_type == "reflect":
                fl = math.floor(tn)
                tn = tn - fl
                if int(fl) % 2:
                    tn = 1.0 - tn
            else:
                tn = min(1.0, max(0.0, tn))
            rgba = _lerp_stops(stops, cols, tn)
            for ch in range(4):
                lut[ch].append(max(0, min(255, int(round(rgba[ch] * 255)))))
        return Image.merge("RGBA", [full.point(lut[ch]) for ch in range(4)])

    def _pattern_rgba(self, shd, W, H, bbox):
        """底纹（8.3.3）：把 CellContent 画成一个小图，按 XStep/YStep 平铺。

        ReflectMethod 控制相邻格是否镜像（Row 按行镜像、Column 按列镜像、
        RowAndColumn 两者都镜像），与 PDF 的 tiling pattern 语义一致。
        """
        pt = shd.get("pattern") or {}
        ops = shd.get("cell_ops") or []
        if not pt or not ops:
            return None
        k = self.work_dpi / 25.4
        cw = max(1, int(round(pt["w"] * k)))
        ch = max(1, int(round(pt["h"] * k)))
        xs = max(1, int(round(pt["xstep"] * k)))
        ys = max(1, int(round(pt["ystep"] * k)))

        # 1) 把 cell 画到一张小图上（用独立的 Renderer 实例，避免污染主 warnings）
        sub = PilRenderer.__new__(PilRenderer)
        sub.__dict__.update(self.__dict__)
        sub.dpi = self.work_dpi
        sub.ss = 1
        sub._buf = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        sub.warnings = []
        sub._warned_join = True          # 底纹内部的告警不重复刷屏
        d = ImageDraw.Draw(sub._buf, "RGBA")
        for op in ops:
            try:
                sub._paint(op.get("t"), d, sub._buf, op, k)
            except Exception as exc:
                self.warnings.append("底纹单元绘制失败: %s" % exc)
                break
        cell = sub._buf
        if cell.getbbox() is None:
            return None

        # 2) 平铺到整幅（只在 bbox 范围内，省内存）
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        rf = (pt.get("reflect") or "normal").lower()
        x0 = max(0, int(bbox[0] * k) - xs) if bbox else 0
        y0 = max(0, int(bbox[1] * k) - ys) if bbox else 0
        x1 = min(W, int(bbox[2] * k) + xs) if bbox else W
        y1 = min(H, int(bbox[3] * k) + ys) if bbox else H
        col = 0
        x = x0
        while x < x1 + xs:
            row = 0
            y = y0
            while y < y1 + ys:
                tile = cell
                if rf in ("row", "rowandcolumn") and (row % 2) == 1:
                    tile = tile.transpose(Image.FLIP_TOP_BOTTOM)
                if rf in ("column", "rowandcolumn") and (col % 2) == 1:
                    tile = tile.transpose(Image.FLIP_LEFT_RIGHT)
                out.alpha_composite(tile, (x, y)) if hasattr(out, "alpha_composite") \
                    else out.paste(tile, (x, y), tile)
                y += ys
                row += 1
            x += xs
            col += 1
        return out

    def _gouraud_rgba(self, shd, W, H, bbox=None):
        """高洛德渐变（8.3.4.4）。

        标准给的公式：
            VP = (VA·S_PBC + VB·S_PAC + VC·S_PAB) / S_ABC
        也就是**重心坐标**插值。重心坐标对 P 是线性的，所以每个三角形都能用
        两次仿射变换得到 λ1、λ2（λ3 = 1-λ1-λ2），再把三色加权相加。

        多个 Point 按 3 个一组分三角形（标准规定 Point 至少 3 个）；
        EdgeFlag 决定相邻三角形的接续方向，取色规则一致，这里不改变插值本身。
        三角区域外的点：Extend=0 不填充，=1 用 BackColor。
        """
        pts = self._shading_points(shd)
        if len(pts) < 3:
            return None
        k = self.work_dpi / 25.4
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        touched = Image.new("L", (W, H), 0)
        back = shd.get("back")
        extend = int(shd.get("extend") or 0)

        # 按 3 个一组分三角形
        for i in range(0, len(pts) - 2, 3):
            tri = pts[i:i + 3]
            if len(tri) < 3:
                break
            self._paint_triangle(out, touched, tri, k)

        if extend and back is not None:
            # 三角区域外的部分用 BackColor 填（Extend=1）
            inv = touched.point(lambda v: 255 - v)
            fill = tuple(int(round(c * 255)) for c in back)
            solid = Image.new("RGBA", (W, H), fill)
            out.paste(solid, (0, 0), inv)
        return out

    @staticmethod
    def _shading_points(shd):
        """把 Shading 里解析出的控制点转成 [(x, y, rgba)]（mm）。"""
        out = []
        for pt in shd.get("points") or []:
            out.append((pt[0], pt[1], pt[2]))
        return out

    def _lattice_rgba(self, shd, W, H, bbox=None):
        """网格高洛德渐变（8.3.4.5）。

        与普通高洛德的唯一区别是点的**组织方式**：按 VerticesPerRow 每行固定
        网格数，相邻 4 个点（左上、右上、左下、右下）组成一个网格单元，
        单元内再拆成两个三角形插值。点数不足 4 或没给 VerticesPerRow 时
        退回普通高洛德（按 3 点分组）。
        """
        pts = self._shading_points(shd)
        vpr = int(shd.get("vpr") or 0)
        if vpr < 2 or len(pts) < 4:
            return self._gouraud_rgba(shd, W, H, bbox)

        k = self.work_dpi / 25.4
        rows = len(pts) // vpr
        if rows < 2:
            return self._gouraud_rgba(shd, W, H, bbox)

        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        touched = Image.new("L", (W, H), 0)
        for r in range(rows - 1):
            for c in range(vpr - 1):
                tl = pts[r * vpr + c]
                tr = pts[r * vpr + c + 1]
                bl = pts[(r + 1) * vpr + c]
                br = pts[(r + 1) * vpr + c + 1]
                # 每个格子两个三角形，保证整格被覆盖且不重叠
                self._paint_triangle(out, touched, [tl, tr, br], k)
                self._paint_triangle(out, touched, [tl, br, bl], k)

        back = shd.get("back")
        if int(shd.get("extend") or 0) and back is not None:
            inv = touched.point(lambda v: 255 - v)
            out.paste(Image.new("RGBA", (W, H),
                                tuple(int(round(c2 * 255)) for c2 in back)), (0, 0), inv)
        return out

    def _paint_triangle(self, out, touched, tri, k):
        """画一个高洛德三角形，累加到 out 上。"""
        (ax, ay, ca), (bx, by, cb), (cx, cy, cc) = tri
        # 面积判零（退化成线/点时跳过）
        d = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if abs(d) < 1e-9:
            return

        xs = [ax * k, bx * k, cx * k]
        ys = [ay * k, by * k, cy * k]
        x0 = max(0, int(math.floor(min(xs))) - 1)
        y0 = max(0, int(math.floor(min(ys))) - 1)
        x1 = min(out.size[0], int(math.ceil(max(xs))) + 1)
        y1 = min(out.size[1], int(math.ceil(max(ys))) + 1)
        if x1 <= x0 or y1 <= y0:
            return
        w, h = x1 - x0, y1 - y0

        # λ1 = S_PBC / S_ABC，λ2 = S_PAC / S_ABC —— 都对 P 线性
        # 用 (px, py) 的像素坐标表示，先转成 mm 再算系数
        def lam(px_, py_):
            # 重心坐标（标准 8.3.4.4 的面积比）：
            #   λ_A = S_PBC / S_ABC = ((B-P)×(C-P)) / ((B-A)×(C-A))
            # 注意分子用 (B-P)/(C-P)、不是 (P-B)/(C-B)：叉积差一个负号，
            # 写反会让三角形内的重心坐标全变负，整块只剩第三色。
            mx = (px_ + 0.5) / k
            my = (py_ + 0.5) / k
            l1 = ((bx - mx) * (cy - my) - (by - my) * (cx - mx)) / d
            l2 = ((cx - mx) * (ay - my) - (cy - my) * (ax - mx)) / d
            return l1, l2

        # 仿射：src_x = A*dst_x + B*dst_y + C
        # λ 在 mm 下线性，转成像素坐标后仍线性，取三个点解出系数
        # λ 对 (x, y) 各取一次增量，四个都要取对方向：
        # A 是沿 x 的增量、B 是沿 y 的增量。早期把 λ2 的两个增量取反了方向，
        # 结果 λ2 近似常数、λ3 恒等于 1，整块显示成第三色。
        l1_00, l2_00 = lam(x0, y0)
        l1_10, l2_10 = lam(x0 + 1, y0)      # 沿 x
        l1_01, l2_01 = lam(x0, y0 + 1)      # 沿 y
        A1, B1 = l1_10 - l1_00, l1_01 - l1_00
        A2, B2 = l2_10 - l2_00, l2_01 - l2_00
        C1, C2 = l1_00, l2_00

        def field(A, B, C):
            rows = []
            for y in range(h):
                rows.append(bytes([max(0, min(255, int(round((A * x + B * y + C) * 255))))
                                   for x in range(w)]))
            return Image.frombytes("L", (w, h), b"".join(rows))

        # λ 场：三个重心坐标
        l1_img = field(A1, B1, C1)
        l2_img = field(A2, B2, C2)
        l3_img = ImageChops.subtract(ImageChops.subtract(
            Image.new("L", (w, h), 255), l1_img), l2_img)

        # 三角形掩码
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).polygon(
            [(xs[0] - x0, ys[0] - y0), (xs[1] - x0, ys[1] - y0), (xs[2] - x0, ys[2] - y0)],
            fill=255)

        rgba = []
        for ch in range(4):
            c1 = Image.new("L", (w, h), int(round(ca[ch] * 255)))
            c2 = Image.new("L", (w, h), int(round(cb[ch] * 255)))
            c3 = Image.new("L", (w, h), int(round(cc[ch] * 255)))
            acc = ImageChops.add(
                ImageChops.add(ImageChops.multiply(l1_img, c1),
                               ImageChops.multiply(l2_img, c2)),
                ImageChops.multiply(l3_img, c3))
            rgba.append(acc)

        tri_img = Image.merge("RGBA", rgba)
        out.paste(tri_img, (x0, y0), mask)
        touched.paste(Image.new("L", (w, h), 255), (x0, y0), mask)

    def _fill_with_shading(self, draw, op, scale, subpaths, closed):
        """用渐变填一个（可能由多条子路径组成的）区域。"""
        shd = op.get("fill")
        W, H = self._canvas_size()

        # 先算 bbox（径向渐变要按它造覆盖整块的距离场），再生成渐变图
        xs, ys = [], []
        for arr in subpaths:
            for j in range(0, len(arr) - 1, 2):
                xs.append(arr[j])
                ys.append(arr[j + 1])
        bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None

        layer = self._shading_rgba(shd, W, H, bbox)
        if layer is None:
            self.warnings.append("渐变生成失败，已跳过该填充")
            return

        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        for arr in subpaths:
            xy = [(_px(arr[k], self.work_dpi), _px(arr[k + 1], self.work_dpi))
                  for k in range(0, len(arr) - 1, 2)]
            if len(xy) >= 3:
                md.polygon(xy, fill=255)
        # 渐变图本身可能有大片透明区（底纹单元之间的空隙、径向的外部），
        # 只用对象形状当 mask 会把透明处按黑色贴上去。要和图自身的 alpha 求交。
        if layer.mode == "RGBA":
            alpha = layer.getchannel("A")
            mask = ImageChops.multiply(mask, alpha)
        self._buf.paste(layer, (0, 0), mask)

    # ---------------------------------------------------------- 裁剪
    def _paint(self, t, draw, img, op, scale):
        if t == "rect":
            self._draw_rect(draw, op, scale)
        elif t == "path":
            self._draw_path(draw, op, scale)
        elif t == "text":
            self._draw_text(img, op, scale)
        elif t == "image":
            self._draw_image(img, op, scale)

    def _clip_mask(self, groups, W, H):
        """按标准把裁剪区算成一张掩码。

        表34：多个 Clip **取交集**；表33：一个 Clip 内多个 Area **取并集**。
        并集在同一次 polygon 填充里自然成立，交集用 darker（取较小值）逐组累积。
        """
        mask = None
        for g in groups:
            m = Image.new("L", (W, H), 0)
            d = ImageDraw.Draw(m)
            for pts in g:
                xy = [(_px(pts[k], self.work_dpi), _px(pts[k + 1], self.work_dpi))
                      for k in range(0, len(pts) - 1, 2)]
                if len(xy) >= 3:
                    d.polygon(xy, fill=255)
            mask = m if mask is None else ImageChops.darker(mask, m)
        return mask

    def __init__(self, dpi: int = _DEFAULT_DPI, background=(255, 255, 255),
                 supersample: int = _DEFAULT_SUPERSAMPLE):
        self.dpi = max(36, min(1200, int(dpi)))
        self.ss = max(1, int(supersample))
        self.work_dpi = self.dpi * self.ss
        self.background = background
        self._font_cache: Dict[tuple, ImageFont.FreeTypeFont] = {}
        self._image_cache: Dict[str, Image.Image] = {}
        self.warnings: List[str] = []

    # ---------------------------------------------------------- 字体
    def _font(self, family: str, size_px: float, bold: bool, italic: bool,
              mono: bool = False):
        key = (family, round(size_px, 1), bold, italic, bool(mono))
        cached = self._font_cache.get(key)
        if cached is not None:
            return cached
        path = font_manager.resolve(family, bold, italic, mono=mono)
        if path is None:
            self.warnings.append("系统里没有可用字体，文本以默认位图字体绘制")
            f = ImageFont.load_default()
            self._font_cache[key] = f
            return f
        try:
            f = ImageFont.truetype(path, max(1, int(round(size_px))))
        except Exception:
            f = ImageFont.load_default()
        self._font_cache[key] = f
        return f

    # ---------------------------------------------------------- 主流程
    def render(self, scene: PageScene) -> Image.Image:
        w_mm, h_mm = scene.size.w, scene.size.h
        if w_mm <= 0 or h_mm <= 0:
            raise RenderError("页面尺寸非法: %sx%s mm" % (w_mm, h_mm))
        # 超采样：先在 work_dpi 下画，最后缩到 dpi
        W = max(1, int(round(_px(w_mm, self.work_dpi))))
        H = max(1, int(round(_px(h_mm, self.work_dpi))))
        img = Image.new("RGB", (W, H), self.background)
        self._buf = img          # 渐变填充要按画布尺寸生成，记一下
        draw = ImageDraw.Draw(img, "RGBA")
        scale = self.work_dpi / 25.4

        for op in scene.ops:
            t = op.get("t")
            clip = op.get("clip")
            try:
                if clip:
                    # 8.4：落在裁剪区以外的部分不受绘制指令影响。
                    # Pillow 没有 clip 状态，只能先画到透明图层再按掩码贴回去。
                    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                    ldraw = ImageDraw.Draw(layer, "RGBA")
                    self._paint(t, ldraw, layer, op, scale)
                    mask = self._clip_mask(clip, W, H)
                    if mask is not None:
                        img.paste(layer, (0, 0), mask)
                    else:
                        self.warnings.append("裁剪区生成失败，按不裁剪绘制")
                        img.paste(layer, (0, 0), layer)
                else:
                    self._paint(t, draw, img, op, scale)
            except Exception as exc:
                self.warnings.append("绘制 %s 失败: %s" % (t, exc))

        if self.ss > 1:
            out_w = max(1, int(round(_px(w_mm, self.dpi))))
            out_h = max(1, int(round(_px(h_mm, self.dpi))))
            img = img.resize((out_w, out_h), Image.LANCZOS)
        return img

    # ---------------------------------------------------------- 基元
    def _color(self, rgba) -> Optional[tuple]:
        if not rgba:
            return None
        r, g, b, a = rgba
        return (int(r * 255), int(g * 255), int(b * 255), int(max(0.0, min(1.0, a)) * 255))

    def _draw_rect(self, draw: ImageDraw.ImageDraw, op: dict, scale: float) -> None:
        x0 = _px(op["x"], self.work_dpi)
        y0 = _px(op["y"], self.work_dpi)
        x1 = _px(op["x"] + op["w"], self.work_dpi)
        y1 = _px(op["y"] + op["h"], self.work_dpi)
        fill = self._color(op.get("fill"))
        stroke = self._color(op.get("stroke"))
        draw.rectangle([x0, y0, x1, y1], fill=fill,
                       outline=stroke, width=1 if stroke else 0)

    def _draw_path(self, draw: ImageDraw.ImageDraw, op: dict, scale: float) -> None:
        raw_fill = op.get("fill")
        shading = raw_fill if isinstance(raw_fill, dict) else None
        fill = None if shading else self._color(raw_fill)
        stroke = self._color(op.get("stroke"))
        width = _stroke_px(op.get("w", 0.2), self.work_dpi)
        subs = op.get("subs") or []
        closed_flags = op.get("closed") or [False] * len(subs)
        if shading is not None:
            # 渐变按整个对象区域填一次，不逐子路径填——否则子路径之间会有接缝
            self._fill_with_shading(draw, op, scale, subs, closed_flags)

        for i, arr in enumerate(subs):
            pts = [(_px(arr[k], self.work_dpi), _px(arr[k + 1], self.work_dpi))
                   for k in range(0, len(arr) - 1, 2)]
            if len(pts) < 2:
                continue
            closed = closed_flags[i] if i < len(closed_flags) else False
            if fill is not None and (closed or len(pts) > 2):
                draw.polygon(pts, fill=fill)
            if stroke is not None:
                seq = pts + [pts[0]] if closed else pts
                dash = op.get("dash")
                if dash:
                    # dash / offset 在 scene 里都是 mm，这里换算成像素
                    scaled = [max(0.5, float(d) * scale) for d in dash]
                    off = float(op.get("dash_offset") or 0.0) * scale
                    for piece in _dash_segments(seq, scaled, off):
                        if len(piece) >= 2:
                            draw.line(piece, fill=stroke, width=width, joint="curve")
                else:
                    # 8.2.2：Pillow 的 joint 只支持 "curve"（圆角），
                    # Miter / Bevel 无法精确表达，按圆角近似并告警一次
                    jn = (op.get("join") or "miter").lower()
                    if jn != "round" and not self._warned_join:
                        self.warnings.append(
                            "线条连接样式 %s 在 Pillow 后端按圆角近似（Qt 与 Canvas 是准确的）" % jn)
                        self._warned_join = True
                    draw.line(seq, fill=stroke, width=width, joint="curve")

                # 8.2.5：端点样式。Pillow 没有 cap 概念，按值补画端点
                cp = (op.get("cap") or "butt").lower()
                if width > 1 and cp in ("round", "square"):
                    r = width / 2.0
                    ends = [seq[0], seq[-1]]
                    if cp == "round":
                        for px, py in ends:
                            draw.ellipse([px - r, py - r, px + r, py + r], fill=stroke)
                    else:   # square：沿线段方向外扩半个线宽
                        for i, (px, py) in enumerate(ends):
                            qx, qy = (seq[1] if i == 0 else seq[-2])
                            dx, dy = px - qx, py - qy
                            ln = math.hypot(dx, dy) or 1.0
                            ox, oy = dx / ln * r, dy / ln * r
                            draw.rectangle([px - r + ox, py - r + oy,
                                            px + r + ox, py + r + oy], fill=stroke)
        del scale

    def _draw_text(self, img: Image.Image, op: dict, scale: float) -> None:
        text = op.get("text") or ""
        if not text:
            return
        color = self._color(op.get("color")) or (0, 0, 0, 255)
        size_px = max(1.0, op.get("size", 3.0) * scale)
        font = self._font(op.get("font") or "", size_px, bool(op.get("bold")),
                          bool(op.get("italic")), mono=bool(op.get("fixed_width")))
        x = _px(op["x"], self.work_dpi)
        y = _px(op["y"], self.work_dpi)
        rot = float(op.get("rot") or 0.0)
        spacing = op.get("spacing") or 0.0

        if abs(rot) < 0.01 and not spacing:
            draw = ImageDraw.Draw(img, "RGBA")
            draw.text((x, y), text, font=font, fill=color, anchor="ls")
            return

        # 需要旋转或字距：先画到透明层，再旋转贴回
        try:
            bbox = font.getbbox(text, anchor="ls")
        except Exception:
            bbox = (0, 0, int(size_px * len(text)), int(size_px))
        tw = max(1, int(bbox[2] - bbox[0] + size_px))
        th = max(1, int(bbox[3] - bbox[1] + size_px * 2))
        layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        if spacing:
            cx = 0.0
            for ch in text:
                ld.text((cx, th - size_px * 0.4), ch, font=font, fill=color, anchor="ls")
                cx += spacing * scale + ld.textlength(ch, font=font)
        else:
            ld.text((0, th - size_px * 0.4), text, font=font, fill=color, anchor="ls")
        if abs(rot) > 0.01:
            layer = layer.rotate(rot, expand=True, resample=Image.BICUBIC)
        # 以基线左端为锚点贴回
        base_y = y - (th - size_px * 0.4)
        img.paste(layer, (int(x), int(base_y)), layer)

    def _draw_image_border(self, img, op, bd):
        """画图像边框（圆角 + 虚线），起点左上角、顺时针（表43）。"""
        x = _px(op["x"], self.work_dpi)
        y = _px(op["y"], self.work_dpi)
        w = _px(op["w"], self.work_dpi)
        h = _px(op["h"], self.work_dpi)
        lw = _stroke_px(bd["w"], self.work_dpi)
        col = self._color(bd["color"])
        if col is None:
            return
        rh = bd.get("radius_h") or 0.0
        rv = bd.get("radius_v") or 0.0
        if rh > 0 or rv > 0:
            rhp = max(0.0, min(w / 2.0, _px(rh, self.work_dpi)))
            rvp = max(0.0, min(h / 2.0, _px(rv, self.work_dpi)))
            # 用四段圆弧拼圆角矩形（弧度不同时按椭圆弧画）
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            d.rounded_rectangle([x, y, x + w, y + h], radius=min(rhp, rvp),
                                outline=col, width=lw)
            img.paste(layer, (0, 0), layer)
        else:
            d2 = ImageDraw.Draw(img, "RGBA")
            dash = bd.get("dash")
            if dash:
                # 表43：边框起点在左上角、绕行方向顺时针
                seq = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
                scaled = [max(0.5, float(v) * self.work_dpi / 25.4) for v in dash]
                off = float(bd.get("dash_offset") or 0.0) * self.work_dpi / 25.4
                for piece in _dash_segments(seq, scaled, off):
                    if len(piece) >= 2:
                        d2.line(piece, fill=col, width=lw)
            else:
                d2.rectangle([x, y, x + w, y + h], outline=col, width=lw)

    def _draw_image(self, img: Image.Image, op: dict, scale: float) -> None:
        res = op.get("res")
        if not res:
            return
        cached = self._image_cache.get(res)
        if cached is None:
            self.warnings.append("图片资源 %s 需要由调用方注入" % res)
            return
        b = op.get("corners") or []
        if len(b) != 4:
            return
        x = _px(op["x"], self.work_dpi)
        y = _px(op["y"], self.work_dpi)
        w = max(1, int(round(_px(op["w"], self.work_dpi))))
        h = max(1, int(round(_px(op["h"], self.work_dpi))))
        im = cached.convert("RGBA")
        im = im.resize((w, h), Image.LANCZOS)
        alpha = op.get("alpha", 1.0)
        if alpha < 1.0:
            a_chan = im.getchannel("A").point(lambda v: int(v * alpha))
            im.putalpha(a_chan)
        img.paste(im, (int(x), int(y)), im)

        # 表43：Border 边框。线宽为 0 已在 model 层判掉（返回 None）。
        bd = op.get("border")
        if bd:
            self._draw_image_border(draw if False else img, op, bd)

    def load_resources(self, resources: Dict[str, dict]) -> None:
        """把 scene.resources 里的图片解码进缓存。"""
        for key, meta in resources.items():
            data = meta.get("data") or ""
            if not data:
                continue
            try:
                raw = base64.b64decode(data)
                self._image_cache[key] = Image.open(io.BytesIO(raw))
            except Exception as exc:
                self.warnings.append("图片 %s 解码失败: %s" % (key, exc))


# ------------------------------------------------------------------ 便捷函数

def render_page(page, dpi: int = _DEFAULT_DPI):  # -> (PIL.Image, warnings)
    """渲染单页，自动注入页面图片资源。"""
    from .scene import build_page_scene
    sc = build_page_scene(page)
    r = PilRenderer(dpi=dpi)
    r.load_resources(sc.resources)
    img = r.render(sc)
    return img, r.warnings + sc.warnings


def render_to_png(scene: PageScene, resources: Optional[dict] = None,
                  dpi: int = _DEFAULT_DPI) -> bytes:
    r = PilRenderer(dpi=dpi)
    r.load_resources(resources if resources is not None else scene.resources)
    img = r.render(scene)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_to_pdf_bytes(pages: List[PageScene], resources_by_page: Optional[List[dict]] = None,
                        dpi: int = 200, title: str = "") -> bytes:
    """多页转 PDF。走位图，dpi 决定清晰度；200dpi 打印已足够。"""
    imgs = []
    for i, sc in enumerate(pages):
        res = (resources_by_page[i] if resources_by_page else sc.resources) or {}
        r = PilRenderer(dpi=dpi)
        r.load_resources(res)
        img = r.render(sc)
        if img.mode != "RGB":
            img = img.convert("RGB")
        imgs.append(img)
    if not imgs:
        raise RenderError("没有可导出的页面")
    buf = io.BytesIO()
    first, rest = imgs[0], imgs[1:]
    first.save(buf, format="PDF", resolution=float(dpi), save_all=True,
               append_images=rest, title=title or "OFD")
    return buf.getvalue()

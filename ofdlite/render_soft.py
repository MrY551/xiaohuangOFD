# -*- coding: utf-8 -*-
"""纯 Python 软件渲染器：不依赖 Pillow，全部按标准自己实现。

组件：
  - 画布：bytearray 的 RGBA 缓冲
  - 填充：ofdlite/raster.py 的扫描线（nonzero / evenodd）
  - 描边：ofdlite/stroke.py 的折线转多边形（miter 截断、cap 三档）
  - 导出：ofdlite/pngcodec.py 的 PNG 编码
  - 裁剪：矩形/多边形裁剪，用同一套扫描线算覆盖

为什么自己写：项目原则是「能自己写的对照标准自己写实现」。
Pillow 只是把别人的光栅化包了一层，而 OFD 需要的图形语义（fill rule、
miter 截断、dash 相位）它并不能完整表达（实测 MiterLimit 完全不生效）。

当前只覆盖几何图元（rect / path）。文字与图像资源是后续阶段。

性能：逐像素 Python 循环对大画布很慢，所以
  1) 填充只在对象包围盒内做；
  2) 扫描线的水平跨段用切片赋值，不逐像素；
  3) 抗锯齿暂不做（与 Pillow 后端同为硬边，靠导出时提高 dpi）。
"""
from __future__ import annotations

import base64
import math
from typing import List, Optional, Sequence, Tuple

from . import pngcodec
from . import imgquad
from . import _fast
from .raster import fill_coverage as _py_fill_coverage, fill_mask

def fill_coverage(polys, width, height, rule="nonzero", ss=4):
    """扫描线填充的入口：有 Rust 内核就走 Rust，没有就回落纯 Python。

    两者的输出必须逐字节一致（tests 里有比对），所以上层不需要知道下面是谁。
    """
    return _fast.fill_coverage(polys, width, height, rule, ss)

from .stroke import stroke_to_polygon


def _rotate_canvas(cv, deg):
    """把画布顺时针旋转 deg 度（90/180/270）。"""
    w, h = cv.w, cv.h
    if deg == 180:
        out = Canvas(w, h)
        src, dst = cv.buf, out.buf
        n = w * h
        for i in range(n):
            j = (n - 1 - i) * 4
            k = i * 4
            dst[j:j + 4] = src[k:k + 4]
        return out
    out = Canvas(h, w)          # 90/270 宽高互换
    src, dst = cv.buf, out.buf
    ow = out.w
    for y in range(h):
        for x in range(w):
            if deg == 90:
                nx, ny = h - 1 - y, x
            else:
                nx, ny = y, w - 1 - x
            si = (y * w + x) * 4
            di = (ny * ow + nx) * 4
            dst[di:di + 4] = src[si:si + 4]
    return out



Point = Tuple[float, float]


def _split_dash(pts, pattern, offset=0.0):
    """按 DashPattern 把折线切成若干实线片段（8.2.4）。

    标准表 23：DashPattern 是「实线长、间隔长」交替的数组，可以是两个或多个值
    （8.2.4 原文「有两个或多个值」）；奇数个元素时整体重复一遍（与 PDF 一致）。

    DashOffset：8.2.4 原文「在当前位置按**绘制相反方向**偏移 DashOffset 值指定的
    距离后开始绘制虚线」—— 也就是起点要往回退，等价于相位取负。
    写成 +offset 会让相位整个反过来：offset=10、pattern="30 30" 时
    正确结果是从「间隔」里的第 10mm 开始（先空 20mm 再说），
    写反了则变成从「实线」第 10mm 开始（先画 20mm 实线）。
    """
    if not pattern:
        return [list(pts)]
    pat = [max(0.01, float(v)) for v in pattern]
    if len(pat) % 2:
        pat = pat + pat
    total = sum(pat)
    if total <= 0:
        return [list(pts)]
    # 8.2.4：往绘制相反方向偏移 → 相位取负
    phase = (-float(offset)) % total
    idx = 0
    while phase >= pat[idx % len(pat)]:
        phase -= pat[idx % len(pat)]
        idx += 1
    solid = (idx % 2) == 0
    remain = pat[idx % len(pat)] - phase

    out = []
    cur = [pts[0]]
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg <= 1e-12:
            continue
        dx = (x1 - x0) / seg
        dy = (y1 - y0) / seg
        t = 0.0
        while t < seg - 1e-12:
            step = min(remain, seg - t)
            t += step
            remain -= step
            px = x0 + dx * t
            py = y0 + dy * t
            if solid:
                cur.append((px, py))
            if remain <= 1e-9:
                if solid and len(cur) >= 2:
                    out.append(cur)
                solid = not solid
                idx += 1
                remain = pat[idx % len(pat)]
                cur = [(px, py)] if solid else []
    if solid and len(cur) >= 2:
        out.append(cur)
    return out


def _shift_ops(ops, ox, oy):
    """把整页图元按 (-ox, -oy) 平移。

    表 7：PhysicalBox 的左上角坐标就是页面空间的原点，所以它写了
    "10 20 200 300" 时，页面空间里的 (0,0) 落在画布左上角，
    内容坐标要统一减掉 (10, 20)。
    """
    if not ox and not oy:
        return ops
    out = []
    for op in ops:
        t = op.get("t")
        d = dict(op)
        if t in ("rect", "text", "image"):
            if "x" in d:
                d["x"] = d["x"] - ox
            if "y" in d:
                d["y"] = d["y"] - oy
        if t == "path" and d.get("subs"):
            d["subs"] = [[v - (ox if k % 2 == 0 else oy)
                          for k, v in enumerate(arr)] for arr in d["subs"]]
        if t == "image" and d.get("corners"):
            d["corners"] = [[p[0] - ox, p[1] - oy] for p in d["corners"]]
        if d.get("clip"):
            d["clip"] = [[[v - (ox if k % 2 == 0 else oy)
                           for k, v in enumerate(arr)] for arr in grp]
                         for grp in d["clip"]]
        out.append(d)
    return out


class Canvas(object):
    """RGBA 画布（行优先，每像素 4 字节）。坐标单位是像素。"""

    __slots__ = ("w", "h", "buf", "off")

    def __init__(self, width: int, height: int, bg=(255, 255, 255, 255)):
        self.w = int(width)
        self.h = int(height)
        self.buf = bytearray(bg * (self.w * self.h))
        # 绘制原点偏移（像素）。裁剪渲染会先画到一块只覆盖裁剪区的小画布上，
        # 这时要把页面坐标平移掉，否则内容全落在小画布外面 —— 之前
        # _render_clipped 设了 SoftRenderer._offset 但没人读，裁剪区永远是空的。
        self.off = (0, 0)

    # ---------------------------------------------------------- 基础
    def clear(self, rgba=(255, 255, 255, 255)) -> None:
        self.buf = bytearray(rgba * (self.w * self.h))

    def blend_pixel(self, x: int, y: int, rgba) -> None:
        """把一个像素与已有内容做 source-over 混合。"""
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return
        sa = rgba[3]
        if sa == 0:
            return
        i = (y * self.w + x) * 4
        buf = self.buf
        if sa == 255:
            buf[i] = rgba[0]
            buf[i + 1] = rgba[1]
            buf[i + 2] = rgba[2]
            buf[i + 3] = 255
            return
        da = buf[i + 3]
        # 简化：目标按不透明处理时直接线性插值，省掉完整 porter-duff
        a = sa / 255.0
        buf[i] = int(buf[i] * (1 - a) + rgba[0] * a)
        buf[i + 1] = int(buf[i + 1] * (1 - a) + rgba[1] * a)
        buf[i + 2] = int(buf[i + 2] * (1 - a) + rgba[2] * a)
        buf[i + 3] = min(255, int(da * (1 - a) + sa))

    # ---------------------------------------------------------- 填充
    def fill_polygons(self, polys: Sequence[Sequence[Point]], rgba,
                      rule: str = "nonzero", ss: int = 4) -> None:
        """按 fill rule 填充多边形（坐标是画布像素）。"""
        if not polys or rgba is None or rgba[3] == 0:
            return
        ox, oy = self.off
        if ox or oy:
            polys = [[(p[0] - ox, p[1] - oy) for p in poly] for poly in polys]
        xs = [p[0] for poly in polys for p in poly]
        ys = [p[1] for poly in polys for p in poly]
        x0 = max(0, int(math.floor(min(xs))) - 1)
        y0 = max(0, int(math.floor(min(ys))) - 1)
        x1 = min(self.w, int(math.ceil(max(xs))) + 2)
        y1 = min(self.h, int(math.ceil(max(ys))) + 2)
        if x1 <= x0 or y1 <= y0:
            return
        local = [[(p[0] - x0, p[1] - y0) for p in poly] for poly in polys]
        # 抗锯齿：用覆盖率（0~255）而不是 0/1 掩码
        mb = fill_coverage(local, x1 - x0, y1 - y0, rule, ss)
        mw = x1 - x0
        buf = self.buf
        # 混合这一段是逐像素循环，A4 一页几百万次迭代 —— 有 Rust 内核就交给它，
        # 输出必须与下面 Python 路径逐字节一致（tests 里比对过）。
        if _fast.blend_rect(buf, mb, mw, y1 - y0, self.w, self.h,
                            x0, y0, rgba):
            return
        sa = rgba[3]
        r, g, b = rgba[0], rgba[1], rgba[2]
        a = sa / 255.0
        inv = 1.0 - a
        opaque = (sa == 255)
        for row in range(y1 - y0):
            base_m = row * mw
            base_c = ((y0 + row) * self.w + x0) * 4
            for col in range(mw):
                cvr = mb[base_m + col]
                if not cvr:
                    continue
                i = base_c + col * 4
                if cvr == 255 and opaque:
                    buf[i] = r
                    buf[i + 1] = g
                    buf[i + 2] = b
                    buf[i + 3] = 255
                else:
                    # 覆盖率折算成实际 alpha
                    fa = (cvr / 255.0) * a
                    if fa >= 1.0:
                        buf[i] = r
                        buf[i + 1] = g
                        buf[i + 2] = b
                        buf[i + 3] = 255
                    else:
                        iv = 1.0 - fa
                        buf[i] = int(buf[i] * iv + r * fa)
                        buf[i + 1] = int(buf[i + 1] * iv + g * fa)
                        buf[i + 2] = int(buf[i + 2] * iv + b * fa)
                        na = int(sa * cvr / 255)
                        if buf[i + 3] < na:
                            buf[i + 3] = na

    def fill_rect(self, x: float, y: float, w: float, h: float, rgba) -> None:
        """纯色轴对齐矩形。

        走快速路径：矩形边落在整数像素上时**不需要抗锯齿**，
        直接逐行整段写像素，比算覆盖率快两个数量级。
        整页白底就是这种情形 —— 原来它占整页渲染时间的 37%。
        """
        x0 = int(round(x))
        y0 = int(round(y))
        x1 = int(round(x + w))
        y1 = int(round(y + h))
        aligned = (abs(x - x0) < 1e-6 and abs(y - y0) < 1e-6 and
                   abs(x + w - x1) < 1e-6 and abs(y + h - y1) < 1e-6)
        if aligned and rgba is not None and rgba[3] >= 0.999:
            if x0 < 0:
                x0 = 0
            if y0 < 0:
                y0 = 0
            if x1 > self.w:
                x1 = self.w
            if y1 > self.h:
                y1 = self.h
            if x1 <= x0 or y1 <= y0:
                return
            r = int(rgba[0] * 255 + 0.5) if rgba[0] <= 1.0 else int(rgba[0])
            g = int(rgba[1] * 255 + 0.5) if rgba[1] <= 1.0 else int(rgba[1])
            b = int(rgba[2] * 255 + 0.5) if rgba[2] <= 1.0 else int(rgba[2])
            row = bytes((r, g, b, 255)) * (x1 - x0)
            buf = self.buf
            stride = self.w * 4
            for yy in range(y0, y1):
                o = yy * stride + x0 * 4
                buf[o:o + len(row)] = row
            return
        # 非整数边界 / 半透明：走通用覆盖率路径
        self.fill_polygons([[(x, y), (x + w, y), (x + w, y + h), (x, y + h)]],
                           rgba, "nonzero")

    # ---------------------------------------------------------- 描边
    def stroke_polyline(self, pts: Sequence[Point], width: float, rgba,
                        join: str = "miter", cap: str = "butt",
                        miter_limit: float = 3.528, closed: bool = False) -> None:
        """按 8.2.2 / 8.2.3 / 8.2.5 描边。"""
        polys = stroke_to_polygon(list(pts), width, join, cap,
                                  miter_limit, closed)
        if polys:
            self.fill_polygons(polys, rgba, "nonzero")

    # ---------------------------------------------------------- PIL 兼容
    @property
    def size(self):
        """兼容 PIL.Image.size（上层代码常写 img.size）。"""
        return (self.w, self.h)

    @property
    def mode(self):
        return "RGBA"

    def save(self, path, format=None):
        """兼容 PIL.Image.save。

        PNG 走本项目自己的编码器；JPEG / TIFF 这类格式的**压缩算法**不自己写
        （DCT、LZW 是另一个量级的工程），转成 PNG 字节后交给 Pillow 编码。
        注意：这里借的只是**编码器**，渲染仍是纯 Python，两者是两件事。
        没装 Pillow 时非 PNG 会明确报错，不静默写出坏文件。
        """
        fmt = (format or "").upper()
        is_path = isinstance(path, (str, bytes))
        low = (path.decode() if isinstance(path, bytes) else path).lower() if is_path else ""
        # 目标可能是路径，也可能是文件对象（io.BytesIO）——两种都要支持，
        # 上层有 img.save(buf, "PNG") 这种写法。
        if not is_path:
            if fmt in ("", "PNG"):
                path.write(pngcodec.encode(self.w, self.h, bytes(self.buf), 4))
                return None
            # 非 PNG 的文件对象：转成 PIL 再写
        elif low.endswith(".png") or fmt == "PNG":
            return self.to_png(path)
        try:
            import io as _io
            from PIL import Image
        except ImportError:
            raise ValueError(
                "导出 %r 需要 Pillow 作为编码器（渲染本身不需要它）" % (path,))
        blob = pngcodec.encode(self.w, self.h, bytes(self.buf), 4)
        im = Image.open(_io.BytesIO(blob))
        # JPEG 不支持 alpha 通道：先合成到白底再转 RGB
        if (fmt or "").upper() in ("JPEG", "JPG") or low.endswith((".jpg", ".jpeg")):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        if fmt:
            im.save(path, format=fmt)
        else:
            im.save(path)
        return None

    # ---------------------------------------------------------- 导出
    def to_png(self, path: str, level: int = 6) -> int:
        return pngcodec.write_file(path, self.w, self.h, bytes(self.buf), 4, level)

    def to_rgb_bytes(self) -> bytes:
        """去掉 alpha 通道，返回 RGB 字节（PNG 体积会小 1/4）。"""
        out = bytearray(self.w * self.h * 3)
        buf = self.buf
        j = 0
        for i in range(0, len(buf), 4):
            out[j] = buf[i]
            out[j + 1] = buf[i + 1]
            out[j + 2] = buf[i + 2]
            j += 3
        return bytes(out)


class SoftRenderer(object):
    """把 scene 的绘制指令画到 Canvas 上。

    与 render_pil 的接口保持一致（render_page(page, dpi) -> (image, warnings)），
    方便上层切换。
    """

    def __init__(self, dpi: float = 150.0, ss: int = 4):
        self.dpi = float(dpi)
        # 抗锯齿的垂直子扫描线数（1 = 关闭抗锯齿）
        self.ss = max(1, int(ss))
        self.px_per_mm = self.dpi / 25.4
        self.warnings: List[str] = []
        self._resources: dict = {}
        self._img_cache: dict = {}
        self._font_cache: dict = {}
        self._offset = (0, 0)
        # 内嵌字体：{字体 ID: Font 对象}。标准表 44 的 FontFile 指向包内 ttf/otf，
        # 优先于系统字体——不然字形和原文档不一致。
        self._embedded: dict = {}
        self._doc = None

    def register_package(self, doc) -> None:
        """登记 Document（不是 Package），用于取内嵌字体（FontFile）。

        fonts() / font_file_entry() 是 Document 的方法，不是 Package 的。
        """
        self._doc = doc

    def _embedded_font(self, font_id):
        """按字体 ID 取内嵌字体；没有则返回 None。"""
        if font_id is None or self._doc is None:
            return None
        key = str(font_id)
        if key in self._embedded:
            return self._embedded[key]
        font = None
        try:
            from . import ttf as _ttf
            # Document.find_font(font_id) 是现成入口（model.py:578）；
            # font_file_entry 在 PublicRes 上（FontSpec.owner 指回去）
            spec = self._doc.find_font(key)
            if spec is not None:
                path = None
                owner = getattr(spec, "owner", None)
                if owner is not None and hasattr(owner, "font_file_entry"):
                    path = owner.font_file_entry(spec)
                if path:
                    blob = self._doc.pkg.read(path)
                    if blob:
                        font = _ttf.Font(blob)
        except Exception as exc:
            msg = "内嵌字体 %s 加载失败: %s" % (font_id, exc)
            if msg not in self.warnings:
                self.warnings.append(msg)
        self._embedded[key] = font
        return font

    def render(self, scene: dict) -> Canvas:
        # 表 7：PhysicalBox 左上角即页面空间原点，非零时整体平移
        _org = scene.get("origin") or [0.0, 0.0]
        _ox, _oy = float(_org[0] or 0.0), float(_org[1] or 0.0)
        size = scene.get("size") or [210.0, 297.0]
        rot = int(scene.get("rot") or 0) % 360
        W = max(1, int(round(size[0] * self.px_per_mm)))
        H = max(1, int(round(size[1] * self.px_per_mm)))
        self._resources = scene.get("resources") or {}
        cv = Canvas(W, H)
        k = self.px_per_mm
        _ops = _shift_ops(scene.get("ops") or [], _ox, _oy)
        for op in _ops:
            t = op.get("t")
            try:
                def _draw(target, _op=op, _t=t):
                    if _t == "rect":
                        self._rect(target, _op, k)
                    elif _t == "path":
                        self._path(target, _op, k)
                    elif _t == "text":
                        self._text(target, _op, k)
                    elif _t == "image":
                        self._image(target, _op, k)
                # 8.4：有裁剪就先画到临时画布再抠；没有就直接画
                if not self._render_clipped(cv, op, k, _draw):
                    _draw(cv)
            except Exception as exc:
                self.warnings.append("指令 %s 绘制失败: %s" % (t, exc))
        # 页面旋转（7.x 的 Rotate）：按 90 的倍数转画布。
        # 标准只允许 90/180/270，其它角度是非法值，直接忽略并告警。
        if rot in (90, 180, 270):
            cv = _rotate_canvas(cv, rot)
        elif rot:
            self.warnings.append("页面旋转 %d° 不是 90 的倍数，已忽略" % rot)
        return cv

    # ---------------------------------------------------------- 指令
    @staticmethod
    def _rgba(v) -> Optional[Tuple[int, int, int, int]]:
        if not v or isinstance(v, dict):
            return None
        if len(v) < 4:
            v = list(v) + [1.0]
        return (int(round(max(0.0, min(1.0, v[0])) * 255)),
                int(round(max(0.0, min(1.0, v[1])) * 255)),
                int(round(max(0.0, min(1.0, v[2])) * 255)),
                int(round(max(0.0, min(1.0, v[3])) * 255)))

    def _decode_image(self, res_id: str):
        """把 resources 里的 base64 PNG 解成 (w, h, rgb 或 rgba 字节, channels)。

        只解一次，按 res_id 缓存 —— 同一张图可能被多个 ImageObject 引用。
        """
        if res_id in self._img_cache:
            return self._img_cache[res_id]
        out = None
        try:
            import base64 as _b64
            ent = (self._resources or {}).get(res_id) or {}
            raw = ent.get("data")
            if isinstance(raw, str) and raw:
                blob = _b64.b64decode(raw)
                if blob[:8] == b"\x89PNG\r\n\x1a\n":
                    w, h, px, ch = pngcodec.decode(blob)
                    out = (w, h, px, ch)
                elif blob[:2] == b"\xff\xd8":
                    # 第 10 章不限定图片格式；WPS 导出的真实 OFD 就用 JPEG
                    from . import jpeg as _jpeg
                    w, h, px, ch = _jpeg.decode(blob)
                    out = (w, h, px, ch)
        except Exception as exc:
            self.warnings.append("图片 %s 解码失败: %s" % (res_id, exc))
        self._img_cache[res_id] = out
        return out

    def _image_quad(self, cv, op, k, got):
        """按平行四边形落位（CTM 带旋转/斜切时）。

        仿射变换下矩形 → 平行四边形：
            P = P0 + s·u + t·v    (u = P1-P0, v = P3-P0)
        解 2x2 方程得到 (s,t)，乘源图尺寸就是源坐标，再双线性采样。
        """
        sw, sh, spx, sch = got
        cs = op.get("corners")
        p0 = (cs[0][0] * k, cs[0][1] * k)
        p1 = (cs[1][0] * k, cs[1][1] * k)
        p3 = (cs[3][0] * k, cs[3][1] * k)
        u = (p1[0] - p0[0], p1[1] - p0[1])
        v = (p3[0] - p0[0], p3[1] - p0[1])
        inv = imgquad.quad_inverse(p0, u, v)
        if inv is None:
            msg = "图片 %s 的变换退化成线，已跳过" % (op.get("res") or op.get("id"))
            if msg not in self.warnings:
                self.warnings.append(msg)
            return
        a, b, c, d = inv
        bb = imgquad.bbox_of(cs, k, cv.w, cv.h)
        if bb is None:
            return
        x0, y0, x1, y1 = bb
        alpha = float(op.get("alpha") if op.get("alpha") is not None else 1.0)
        buf = cv.buf
        W = cv.w
        # 四边形在画布上的面积（用于判断点多边形内）
        for dy in range(y0, y1):
            for dx in range(x0, x1):
                rx = dx + 0.5 - p0[0]
                ry = dy + 0.5 - p0[1]
                s = a * rx + b * ry
                t = c * rx + d * ry
                if s < 0.0 or s > 1.0 or t < 0.0 or t > 1.0:
                    continue
                fx = s * (sw - 1)
                fy = t * (sh - 1)
                got_px = imgquad.sample_bilinear(spx, sw, sh, sch, fx, fy)
                if got_px is None:
                    continue
                r, g, b2, sa = got_px
                if sa < 0.5:
                    continue
                fa = (sa / 255.0) * alpha
                if fa <= 0.0:
                    continue
                ri = int(r + 0.5)
                gi = int(g + 0.5)
                bi = int(b2 + 0.5)
                j = (dy * W + dx) * 4
                if fa >= 0.999:
                    buf[j] = ri
                    buf[j + 1] = gi
                    buf[j + 2] = bi
                    buf[j + 3] = 255
                else:
                    iv = 1.0 - fa
                    buf[j] = int(buf[j] * iv + ri * fa)
                    buf[j + 1] = int(buf[j + 1] * iv + gi * fa)
                    buf[j + 2] = int(buf[j + 2] * iv + bi * fa)
                    na = int(sa * alpha)
                    if buf[j + 3] < na:
                        buf[j + 3] = na

    def _image(self, cv: Canvas, op: dict, k: float) -> None:
        """把图片贴到画布上（最近邻缩放 + alpha 合成）。

        OFD 的 ImageObject 用 Boundary 定义落位，这里按 op 的 x/y/w/h 转像素，
        再按目标尺寸从源图采样。缩放在 OFD 里是连续变换，最近邻会有锯齿，
        但保持实现简单且确定（双线性作为可选在 _sample_bilinear 里）。
        """
        got = self._decode_image(op.get("res") or "")
        if not got:
            return
        sw, sh, spx, sch = got
        # 表 43：ImageMask 是与主图同大小的二值图，0 处透明
        mask = None
        mkey = op.get("mask")
        if mkey:
            try:
                mask = self._decode_image(mkey)
            except Exception:
                mask = None
        x0 = int(round(op["x"] * k))
        y0 = int(round(op["y"] * k))
        tw = max(1, int(round(op["w"] * k)))
        th = max(1, int(round(op["h"] * k)))
        if tw <= 0 or th <= 0:
            return
        # CTM 已在 scene 层应用（corners 是变换后的四角）。
        # 轴对齐时走下面更快的矩形路径；非轴对齐（旋转/斜切）时，
        # 仿射变换会把矩形映射成平行四边形，用逆变换反算源坐标。
        cs = op.get("corners") or []
        if len(cs) >= 4 and not imgquad.is_axis_aligned(cs):
            self._image_quad(cv, op, k, got)
            return
        alpha = float(op.get("alpha") if op.get("alpha") is not None else 1.0)
        buf = cv.buf
        W, H = cv.w, cv.h
        # 目标矩形与画布求交，避免越界
        dx0 = max(0, x0)
        dy0 = max(0, y0)
        dx1 = min(W, x0 + tw)
        dy1 = min(H, y0 + th)
        if dx1 <= dx0 or dy1 <= dy0:
            return
        # 双线性采样：源坐标是浮点，取四邻域按权重混合。
        # 最近邻在放大时会出现明显锯齿（监制章那类细线条最明显）。
        fy_step = sh / float(th)
        fx_step = sw / float(tw)
        for dy in range(dy0, dy1):
            fy = (dy - y0) * fy_step
            y0i = int(fy)
            wy = fy - y0i
            if y0i < 0:
                y0i, wy = 0, 0.0
            y1i = y0i + 1 if y0i + 1 < sh else y0i
            row0 = y0i * sw * sch
            row1 = y1i * sw * sch
            crow = (dy * W) * 4
            for dx in range(dx0, dx1):
                fx = (dx - x0) * fx_step
                x0i = int(fx)
                wx = fx - x0i
                if x0i < 0:
                    x0i, wx = 0, 0.0
                x1i = x0i + 1 if x0i + 1 < sw else x0i
                i00 = row0 + x0i * sch
                i01 = row0 + x1i * sch
                i10 = row1 + x0i * sch
                i11 = row1 + x1i * sch
                # 四邻域权重
                w00 = (1 - wx) * (1 - wy)
                w01 = wx * (1 - wy)
                w10 = (1 - wx) * wy
                w11 = wx * wy
                if sch == 4:
                    sa = (spx[i00 + 3] * w00 + spx[i01 + 3] * w01 +
                          spx[i10 + 3] * w10 + spx[i11 + 3] * w11)
                    if mask is not None:
                        sa = sa * self._mask_factor(mask, sw, sh, dx, dy, x0, y0)
                    if sa < 0.5:
                        continue
                    a = (sa / 255.0) * alpha
                    r = (spx[i00] * w00 + spx[i01] * w01 +
                         spx[i10] * w10 + spx[i11] * w11)
                    g = (spx[i00 + 1] * w00 + spx[i01 + 1] * w01 +
                         spx[i10 + 1] * w10 + spx[i11 + 1] * w11)
                    b = (spx[i00 + 2] * w00 + spx[i01 + 2] * w01 +
                         spx[i10 + 2] * w10 + spx[i11 + 2] * w11)
                elif sch == 3:
                    a = alpha
                    r = (spx[i00] * w00 + spx[i01] * w01 +
                         spx[i10] * w10 + spx[i11] * w11)
                    g = (spx[i00 + 1] * w00 + spx[i01 + 1] * w01 +
                         spx[i10 + 1] * w10 + spx[i11 + 1] * w11)
                    b = (spx[i00 + 2] * w00 + spx[i01 + 2] * w01 +
                         spx[i10 + 2] * w10 + spx[i11 + 2] * w11)
                elif sch == 2:
                    av = (spx[i00 + 1] * w00 + spx[i01 + 1] * w01 +
                          spx[i10 + 1] * w10 + spx[i11 + 1] * w11)
                    a = (av / 255.0) * alpha
                    r = g = b = (spx[i00] * w00 + spx[i01] * w01 +
                                 spx[i10] * w10 + spx[i11] * w11)
                else:
                    a = alpha
                    r = g = b = (spx[i00] * w00 + spx[i01] * w01 +
                                 spx[i10] * w10 + spx[i11] * w11)
                if a <= 0:
                    continue
                j = crow + dx * 4
                # 双线性插值出来的是 float，写回 bytearray 前要取整
                ri = int(r + 0.5)
                gi = int(g + 0.5)
                bi = int(b + 0.5)
                if a >= 0.999:
                    buf[j] = ri
                    buf[j + 1] = gi
                    buf[j + 2] = bi
                    buf[j + 3] = 255
                else:
                    inv = 1.0 - a
                    buf[j] = int(buf[j] * inv + ri * a)
                    buf[j + 1] = int(buf[j + 1] * inv + gi * a)
                    buf[j + 2] = int(buf[j + 2] * inv + bi * a)
                    buf[j + 3] = max(buf[j + 3], int(a * 255))
        # 表 43：图片边框（LineWidth 为 0 时不画，标准明文）
        bd = op.get("border")
        if bd:
            self._draw_border(cv, op, k, bd)

    def _shade_color(self, shd, x, y):
        """算某点（画布像素）的渐变颜色，返回 (r,g,b,a)。

        对应 8.3.4 的四种着色：
          axial   沿 start→end 投影，t 归一化后按 Segment 插值
          radial  按到圆心的距离归一化
          gouraud / lattice  重心坐标（每个三角面用 Vertices 的颜色插值）
          pattern 平铺（这里退化成取单元平均色，网格平铺在 _image 路径做）
        """
        kind0 = shd.get("kind")
        segs = shd.get("segments") or []
        # 只有轴向/径向/底纹靠 Segment 插值；高洛德与网格高洛德用的是
        # 控制点颜色（表 31/32），segments 本来就为空，不能因此整块跳过。
        if not segs and kind0 in ("axial", "radial", "pattern"):
            return None
        if not segs:
            segs = [[0.0, [0.0, 0.0, 0.0, 1.0]]]

        def lerp(t):
            t = 0.0 if t < 0 else (1.0 if t > 1 else t)
            if not segs:
                return (0, 0, 0, 255)
            if t <= segs[0][0]:
                c = segs[0][1]
            elif t >= segs[-1][0]:
                c = segs[-1][1]
            else:
                c = segs[-1][1]
                for k in range(1, len(segs)):
                    if t <= segs[k][0]:
                        t0, c0 = segs[k - 1]
                        t1, c1 = segs[k]
                        f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
                        c = [c0[j] + (c1[j] - c0[j]) * f for j in range(4)]
                        break
            return (int(round(max(0.0, min(1.0, c[0])) * 255)),
                    int(round(max(0.0, min(1.0, c[1])) * 255)),
                    int(round(max(0.0, min(1.0, c[2])) * 255)),
                    int(round(max(0.0, min(1.0, c[3])) * 255)))

        kind = shd.get("kind")
        if kind in ("axial", "pattern"):
            sx, sy = shd["start"]
            ex, ey = shd["end"]
            dx, dy = ex - sx, ey - sy
            d2 = dx * dx + dy * dy
            if d2 <= 1e-12:
                return lerp(0.0)
            t = ((x - sx) * dx + (y - sy) * dy) / d2
            mapt = (shd.get("map") or "direct").lower()
            extend = int(shd.get("extend") or 0)
            if mapt in ("repeat", "reflect"):
                axis_len = math.sqrt(d2)
                unit = shd.get("unit") or axis_len
                period = (unit / axis_len) if axis_len > 1e-9 else 1.0
                if period <= 1e-9:
                    period = 1.0
                k = math.floor(t / period)
                tt = (t / period) - k
                if mapt == "reflect" and (k % 2):
                    # 表 29：Reflect 是镜像重复，奇周期要翻转
                    tt = 1.0 - tt
                return lerp(tt)
            # Direct：轴外按 Extend 处理
            if t < 0.0 or t > 1.0:
                # 表 29：Extend=0 不向两侧延伸 —— 轴外不绘制（返回 None 表示跳过）
                # 1 只向起始点外侧、2 只向结束点外侧、3 两侧都用端点色
                if extend == 0:
                    return None
                if t < 0.0 and extend not in (1, 3):
                    return None
                if t > 1.0 and extend not in (2, 3):
                    return None
                # 延伸区用「该方向轴点所在段的颜色」= 端点色
                return lerp(0.0 if t < 0.0 else 1.0)
            return lerp(t)
        if kind == "radial":
            # 表 30：径向渐变是**两个椭圆**之间插值，不是两个圆。
            #   StartPoint / EndPoint   两个椭圆的中心
            #   StartRadius / EndRadius 椭圆的**长半轴**（不是半径）
            #   Eccentricity            离心率：[0,1)，0 退化成圆；短半轴 = a·√(1-e²)
            #   Angle                   椭圆长轴与 x 轴正向夹角（度）
            cx0, cy0 = shd["start"]
            end = shd.get("end") or shd["start"]
            cx1, cy1 = end[0], end[1]
            radii = shd.get("radii") or [0.0, 1.0]
            r0 = float(radii[0]) if radii else 0.0
            r1 = float(radii[1]) if len(radii) > 1 else r0
            if r1 <= 1e-9 and r0 <= 1e-9:
                return lerp(1.0)
            ecc = float(shd.get("ecc") or 0.0)
            if ecc < 0.0:
                ecc = 0.0
            elif ecc > 0.999999:
                ecc = 0.999999
            sq = math.sqrt(1.0 - ecc * ecc)      # 短半轴 / 长半轴
            ang = math.radians(float(shd.get("angle") or 0.0))
            ca, sa = math.cos(-ang), math.sin(-ang)
            # 先把点转到起点坐标系，再沿 y 压扁 1/sq —— 椭圆就变成圆
            dx, dy = x - cx0, y - cy0
            px = dx * ca - dy * sa
            py = (dx * sa + dy * ca) / sq
            ex, ey = cx1 - cx0, cy1 - cy0
            qx = ex * ca - ey * sa
            qy = (ex * sa + ey * ca) / sq
            # 求 t 使 |P - t·Q| = r0 + t·(r1-r0)
            dr = r1 - r0
            qlen2 = qx * qx + qy * qy
            if qlen2 < 1e-12:
                # 同心（两个椭圆中心重合）：直接按到中心的距离算。
                # 这种情况下二次方程会退化成一次甚至定值（r0=0 时 B 也是 0），
                # 必须单独处理，否则 t 恒为 0、整块画成最外圈颜色。
                d = math.hypot(px, py)
                if abs(dr) < 1e-12:
                    return lerp(0.0 if d <= r0 else 1.0)
                return lerp((d - r0) / dr)
            A = qlen2 - dr * dr
            B = -2.0 * (px * qx + py * qy) - 2.0 * r0 * dr
            C = px * px + py * py - r0 * r0
            if abs(A) < 1e-12:
                t = (-C / B) if abs(B) > 1e-12 else 0.0
            else:
                disc = B * B - 4.0 * A * C
                if disc < 0.0:
                    t = 0.0
                else:
                    sd = math.sqrt(disc)
                    t1 = (-B + sd) / (2.0 * A)
                    t2 = (-B - sd) / (2.0 * A)
                    t = t1 if t1 >= 0.0 else t2
            return lerp(t)
        if kind in ("gouraud", "lattice"):
            return self._gouraud_color(shd, x, y)
        return lerp(0.0)

    def _gouraud_color(self, shd, x, y):
        """网格着色：找包含该点的三角面，用重心坐标插值（表 31/32）。"""
        pts = shd.get("points") or []
        segs = shd.get("segments") or []
        if len(pts) < 3:
            return None
        vpr = int(shd.get("vpr") or 0)

        def tri(a, b, c):
            ax, ay = a[0], a[1]
            bx, by = b[0], b[1]
            cx2, cy2 = c[0], c[1]
            d = (by - cy2) * (ax - cx2) + (cx2 - bx) * (ay - cy2)
            if abs(d) < 1e-12:
                return None
            l1 = ((by - cy2) * (x - cx2) + (cx2 - bx) * (y - cy2)) / d
            l2 = ((cy2 - ay) * (x - cx2) + (ax - cx2) * (y - cy2)) / d
            l3 = 1.0 - l1 - l2
            if l1 < -0.001 or l2 < -0.001 or l3 < -0.001:
                return None
            out = []
            for i in (0, 1, 2, 3):
                v = (a[2][i] * l1 + b[2][i] * l2 + c[2][i] * l3)
                out.append(int(round(max(0.0, min(1.0, v)) * 255)))
            return tuple(out)

        # 表 31：EdgeFlag 决定网格单元用哪条对角线切两个三角形。
        # 0（或未给）走「左上-右下」，1 走「右上-左下」。
        # 表 32 明说网格高洛德的 EdgeFlag 固定为 1。
        ef = shd.get("edgeflag")
        # 8.3.4.5 正文：网格高洛德「在一个网格单元内 EdgeFlag 固定为 1」。
        # 实测 EdgeFlag=1 用「左上-右下」对角线能正常覆盖整格，
        # 而换成「右上-左下」会有文件整块画不出来 —— 所以 1 对应 tl-br、
        # 0 对应另一条。原先把 1/0 搞反了，导致网格渐变一给 EdgeFlag 就空白。
        use_tr_bl = (ef == 0)

        if vpr >= 2:
            rows = len(pts) // vpr
            for r in range(rows - 1):
                for c in range(vpr - 1):
                    tl = pts[r * vpr + c]
                    tr = pts[r * vpr + c + 1]
                    bl = pts[(r + 1) * vpr + c]
                    br = pts[(r + 1) * vpr + c + 1]
                    if use_tr_bl:
                        got = tri(tl, tr, bl) or tri(tr, br, bl)
                    else:
                        got = tri(tl, tr, br) or tri(tl, br, bl)
                    if got:
                        return got
        else:
            for i in range(1, len(pts) - 1):
                got = tri(pts[0], pts[i], pts[i + 1])
                if got:
                    return got

        # 表 31：Extend=1 表示「控制点范围外的部分要填充」，用 BackColor；
        # Extend=0（默认）表示不填充，返回 None 让调用方跳过该像素。
        try:
            ext = int(shd.get("extend") or 0)
        except (TypeError, ValueError):
            ext = 0
        if ext == 1:
            back = shd.get("back")
            if back:
                return tuple(int(round(max(0.0, min(1.0, float(v))) * 255))
                             for v in back[:4])
            return (255, 255, 255, 255)
        back = shd.get("back")
        if back:
            return (int(round(back[0] * 255)), int(round(back[1] * 255)),
                    int(round(back[2] * 255)), int(round(back[3] * 255)))
        # 表 31/32：Extend=0（默认）表示「控制点范围之外不填充」——
        # 落到这里说明该点不在任何三角面上，必须返回 None 让调用方跳过，
        # 不能拿控制点的颜色去兜底（那会把整个外接矩形都涂上颜色）。
        try:
            ext0 = int(shd.get("extend") or 0)
        except (TypeError, ValueError):
            ext0 = 0
        if ext0 == 0:
            return None
        # 高洛德/网格高洛德本来就没有 segments，落到这里说明点落不到任何三角面上；
        # 用第一个控制点的颜色兜底，不能去索引一个空列表。
        c = None
        if segs:
            c = segs[0][1]
        elif pts:
            c = pts[0][2]
        if not c:
            c = (0.0, 0.0, 0.0, 1.0)
        return (int(round(c[0] * 255)), int(round(c[1] * 255)),
                int(round(c[2] * 255)), int(round(c[3] * 255)))

    def _fill_shading(self, cv, polys, shd, rule="nonzero"):
        """渐变填充：先用同一套扫描线算出覆盖掩码，再逐像素算颜色。"""
        if not polys or not shd:
            return
        xs = [q[0] for poly in polys for q in poly]
        ys = [q[1] for poly in polys for q in poly]
        x0 = max(0, int(math.floor(min(xs))) - 1)
        y0 = max(0, int(math.floor(min(ys))) - 1)
        x1 = min(cv.w, int(math.ceil(max(xs))) + 2)
        y1 = min(cv.h, int(math.ceil(max(ys))) + 2)
        if x1 <= x0 or y1 <= y0:
            return
        local = [[(q[0] - x0, q[1] - y0) for q in poly] for poly in polys]
        mask = fill_mask(local, x1 - x0, y1 - y0, rule)
        mw = x1 - x0
        buf = cv.buf
        W = cv.w
        cache = {}
        for row in range(y1 - y0):
            my = y0 + row
            base_m = row * mw
            for col in range(mw):
                if not mask[base_m + col]:
                    continue
                mx = x0 + col
                # 同一行内颜色变化平缓，按 (x,y) 缓存意义不大，直接算
                col_rgba = self._shade_color(shd, mx, my)
                if not col_rgba:
                    continue
                r, g, b, a = col_rgba
                if a <= 0:
                    continue
                i = (my * W + mx) * 4
                if a >= 255:
                    buf[i] = r
                    buf[i + 1] = g
                    buf[i + 2] = b
                    buf[i + 3] = 255
                else:
                    f = a / 255.0
                    inv = 1.0 - f
                    buf[i] = int(buf[i] * inv + r * f)
                    buf[i + 1] = int(buf[i + 1] * inv + g * f)
                    buf[i + 2] = int(buf[i + 2] * inv + b * f)
                    if buf[i + 3] < a:
                        buf[i + 3] = a

    def _mask_factor(self, mask, sw, sh, dx, dy, x0, y0):
        """取蒙版在目标像素对应位置的系数（0~1）。蒙版与主图同大小。"""
        try:
            mw, mh, mpx, mch = mask
            if mw <= 0 or mh <= 0:
                return 1.0
            fx = (dx - x0) * mw / float(max(1, sw))
            fy = (dy - y0) * mh / float(max(1, sh))
            mx = int(fx)
            my = int(fy)
            if mx < 0 or my < 0 or mx >= mw or my >= mh:
                return 0.0
            i = (my * mw + mx) * mch
            if mch >= 3:
                # 二值图常存成 RGB，取亮度
                v = (mpx[i] * 299 + mpx[i + 1] * 587 + mpx[i + 2] * 114) // 1000
            else:
                v = mpx[i]
            return v / 255.0
        except Exception:
            return 1.0

    def _draw_border(self, cv, op, k, bd):
        """图片边框（表 43）：矩形容器 + 可选圆角 + 可选虚线。

        LineWidth 默认 0.353mm；**为 0 时不画**（标准明文）。
        HorizonalCornerRadius / VerticalCornerRadius（标准原文拼写就是
        Horizonal，少一个 z）控制圆角；两者不等时是椭圆角。
        """
        w = float(bd.get("w") or 0.0)
        if w <= 0:
            return
        color = self._rgba(bd.get("color")) or (0, 0, 0, 255)
        rh = float(bd.get("radius_h") or 0.0) * k
        rv = float(bd.get("radius_v") or 0.0) * k
        x = float(op["x"]) * k
        y = float(op["y"]) * k
        tw = float(op["w"]) * k
        th = float(op["h"]) * k
        wp = max(1.0, w * k)
        path = []
        if rh > 0.5 or rv > 0.5:
            rh = min(rh, tw / 2.0)
            rv = min(rv, th / 2.0)
            steps = 10
            # 四角各画一段四分之一椭圆
            corners = (
                (x + tw - rh, y + rv, -math.pi / 2, 0),      # 右上
                (x + tw - rh, y + th - rv, 0, math.pi / 2),  # 右下
                (x + rh, y + th - rv, math.pi / 2, math.pi), # 左下
                (x + rh, y + rv, math.pi, math.pi * 1.5),    # 左上
            )
            for cx, cy, a0, a1 in corners:
                for i in range(steps + 1):
                    a = a0 + (a1 - a0) * i / float(steps)
                    path.append((cx + rh * math.cos(a), cy + rv * math.sin(a)))
        else:
            path = [(x, y), (x + tw, y), (x + tw, y + th), (x, y + th)]
        path.append(path[0])

        dash = bd.get("dash") or []
        pat = [float(d) * k for d in dash] if dash else []
        doff = float(bd.get("dash_offset") or 0.0) * k
        if pat:
            for piece in _split_dash(path, pat, doff):
                if len(piece) >= 2:
                    cv.stroke_polyline(piece, wp, color, "bevel", "butt",
                                       3.528, False)
        else:
            cv.stroke_polyline(path, wp, color, "miter", "butt", 3.528, True)

    def _pattern_tile(self, shd, k, origin_x, origin_y, tw, th):
        """把底纹单元（CellContent）渲染成一张 RGBA 贴图。

        表 28：CellContent 用**独立坐标空间**——左上角 (0,0) 为原点、
        X 向右、Y 向下、单位毫米。所以这里直接按 w×h（mm）建画布，
        再把 cell_ops 按同一套 mm→像素比例画上去。
        """
        pat = shd.get("pattern") or {}
        cw = float(pat.get("w") or 8.0)
        ch = float(pat.get("h") or 8.0)
        pw = max(1, int(round(cw * k)))
        ph = max(1, int(round(ch * k)))
        tile = Canvas(pw, ph, (0, 0, 0, 0))
        for op in shd.get("cell_ops") or []:
            try:
                t = op.get("t")
                if t == "rect":
                    self._rect(tile, op, k)
                elif t == "path":
                    self._path(tile, op, k)
                elif t == "text":
                    self._text(tile, op, k)
                elif t == "image":
                    self._image(tile, op, k)
            except Exception:
                pass
        return tile

    def _fill_pattern(self, cv, polys, shd, rule="nonzero", k=1.0):
        """底纹填充（8.3.3）。

        步骤按表 28 的顺序：
          1. 用 CellContent 渲染一个单元
          2. 按 XStep / YStep 在目标区域内平铺
             （若 XStep < Width 或 YStep < Height，按默认值 Width/Height）
          3. ReflectMethod 决定相邻单元是否镜像
          4. 整体用同一套扫描线掩码裁到对象轮廓内
        """
        pat = shd.get("pattern") or {}
        cw = float(pat.get("w") or 8.0)
        ch = float(pat.get("h") or 8.0)
        # 表 28 明文：XStep/YStep 默认等于单元宽高；设得比单元小要按默认处理
        xs = pat.get("xstep")
        ys = pat.get("ystep")
        xstep = float(xs) if xs and float(xs) >= cw else cw
        ystep = float(ys) if ys and float(ys) >= ch else ch
        reflect = (pat.get("reflect") or "normal").lower().replace("_", "")
        tile = self._pattern_tile(shd, k, 0.0, 0.0, cw * k, ch * k)
        tw, th = tile.w, tile.h
        if tw <= 0 or th <= 0:
            return

        # 对象包围盒（像素）
        xsb = [q[0] for poly in polys for q in poly]
        ysb = [q[1] for poly in polys for q in poly]
        x0 = max(0, int(math.floor(min(xsb))) - 1)
        y0 = max(0, int(math.floor(min(ysb))) - 1)
        x1 = min(cv.w, int(math.ceil(max(xsb))) + 2)
        y1 = min(cv.h, int(math.ceil(max(ysb))) + 2)
        if x1 <= x0 or y1 <= y0:
            return
        # 掩码：只填对象轮廓内的部分
        local = [[(q[0] - x0, q[1] - y0) for q in poly] for poly in polys]
        mask = fill_coverage(local, x1 - x0, y1 - y0, rule, self.ss)
        mw = x1 - x0

        # 平铺起点：RelativeTo=Page 时从页面原点起算，Object 时从对象左上角起算
        rel = (pat.get("relative_to") or "object").lower()
        ox = 0.0 if rel == "page" else float(min(xsb))
        oy = 0.0 if rel == "page" else float(min(ysb))
        stepx = xstep * k
        stepy = ystep * k
        if stepx <= 0 or stepy <= 0:
            return

        buf = cv.buf
        W = cv.w
        tiled = tile.buf
        for row in range(y0, y1):
            my = row - oy
            gy = int(math.floor(my / stepy))
            ty_base = my - gy * stepy              # 单元内 y（像素）
            sy = int(ty_base)
            if sy < 0 or sy >= th:
                continue
            # Row / RowAndColumn：奇数行上下镜像
            flip_y = ("row" in reflect) and (gy % 2 == 1)
            if flip_y:
                sy = th - 1 - sy
            if sy < 0 or sy >= th:
                continue
            for col in range(x0, x1):
                mi = mask[(row - y0) * mw + (col - x0)]
                if not mi:
                    continue
                mx = col - ox
                gx = int(math.floor(mx / stepx))
                tx = int(mx - gx * stepx)
                if tx < 0 or tx >= tw:
                    continue
                # Column / RowAndColumn：奇数列左右镜像
                if ("column" in reflect) and (gx % 2 == 1):
                    tx = tw - 1 - tx
                si = (sy * tw + tx) * 4
                sa = tiled[si + 3]
                if not sa:
                    continue
                a = (sa * mi) // 255
                if a <= 0:
                    continue
                di = (row * W + col) * 4
                if a >= 255:
                    buf[di] = tiled[si]
                    buf[di + 1] = tiled[si + 1]
                    buf[di + 2] = tiled[si + 2]
                    buf[di + 3] = 255
                else:
                    iv = 255 - a
                    buf[di] = (buf[di] * iv + tiled[si] * a) // 255
                    buf[di + 1] = (buf[di + 1] * iv + tiled[si + 1] * a) // 255
                    buf[di + 2] = (buf[di + 2] * iv + tiled[si + 2] * a) // 255
                    if buf[di + 3] < a:
                        buf[di + 3] = a

    def _render_clipped(self, cv, op, k, draw_fn):
        """按 op 的 clip 渲染（8.4 裁剪区）。

        clip 是「组」的列表：组内多个子路径取并集，组之间取交集。
        做法：先把对象画到临时画布，再用裁剪掩码抠出来合成——
        这样不必给每种图元都加裁剪参数，几何代码保持简单。
        全程用自写扫描线算掩码，不碰 Pillow。
        """
        clip = op.get("clip")
        if not clip:
            return False
        # clip 的结构是 [组[子路径[扁平坐标 x0,y0,x1,y1,...]]]，
        # 组内取并集、组间取交集（8.4）
        pts_mm = []
        for grp in clip:
            for sub in grp:
                for i in range(0, len(sub) - 1, 2):
                    pts_mm.append((sub[i], sub[i + 1]))
        if not pts_mm:
            return False
        xs = [q[0] * k for q in pts_mm]
        ys = [q[1] * k for q in pts_mm]
        x0 = max(0, int(math.floor(min(xs))) - 1)
        y0 = max(0, int(math.floor(min(ys))) - 1)
        x1 = min(cv.w, int(math.ceil(max(xs))) + 2)
        y1 = min(cv.h, int(math.ceil(max(ys))) + 2)
        if x1 <= x0 or y1 <= y0:
            return True                       # 裁剪区全在页外，不画

        tmp = Canvas(x1 - x0, y1 - y0, (0, 0, 0, 0))
        tmp.off = (x0, y0)          # 让 draw_fn 画到小画布的局部坐标上
        draw_fn(tmp)

        mw, mh = x1 - x0, y1 - y0
        mask = None
        for grp in clip:
            m = bytearray(mw * mh)
            for sub in grp:
                xy = [((sub[i] * k) - x0, (sub[i + 1] * k) - y0)
                      for i in range(0, len(sub) - 1, 2)]
                if len(xy) >= 3:
                    one = fill_mask([xy], mw, mh, "nonzero")
                    for i in range(len(m)):
                        if one[i]:
                            m[i] = 255
            if mask is None:
                mask = m
            else:
                for i in range(len(mask)):
                    mask[i] = 255 if (mask[i] and m[i]) else 0
        if mask is None:
            return True

        src, dst = tmp.buf, cv.buf
        for row in range(mh):
            srow = row * mw
            drow = (y0 + row) * cv.w
            for col in range(mw):
                mi = mask[srow + col]
                if not mi:
                    continue
                si = (srow + col) * 4
                sa = src[si + 3]
                if not sa:
                    continue
                a = (sa * mi) // 255
                if a <= 0:
                    continue
                di = (drow + x0 + col) * 4
                if a >= 255:
                    dst[di] = src[si]
                    dst[di + 1] = src[si + 1]
                    dst[di + 2] = src[si + 2]
                    dst[di + 3] = 255
                else:
                    inv = 255 - a
                    dst[di] = (dst[di] * inv + src[si] * a) // 255
                    dst[di + 1] = (dst[di + 1] * inv + src[si + 1] * a) // 255
                    dst[di + 2] = (dst[di + 2] * inv + src[si + 2] * a) // 255
                    if dst[di + 3] < a:
                        dst[di + 3] = a
        return True

    def _rect(self, cv: Canvas, op: dict, k: float) -> None:
        fill = self._rgba(op.get("fill"))
        if fill is None:
            return
        cv.fill_rect(op["x"] * k, op["y"] * k,
                     op["w"] * k, op["h"] * k, fill)

    def _get_font(self, name: str, font_id=None, mono=False):
        """按 OFD 的字体名取 Font 对象；找不到返回 None（只告警一次）。

        优先用包里内嵌的 FontFile（标准表 44），没有再按名字找系统字体；
        mono 来自表44 的 FixedWidth，只影响**替代**时的偏好（等宽优先）。
        """
        # 内嵌字体优先：它就是原文档用的那套字形
        emb = self._embedded_font(font_id)
        if emb is not None:
            return emb
        key = (name or "").strip()
        ck = (key, bool(mono))
        if ck in self._font_cache:
            return self._font_cache[ck]
        font = None
        path = None
        try:
            from . import fontfind
            from . import ttf as _ttf
            path = fontfind.find(key, mono=mono)
            # .ttf/.ttc 走 glyf，.otf 走 CFF；标准表 44 要求内嵌字体用
            # OpenType，两种轮廓都得支持（ofdlite/cff.py 负责 CFF）
            if path and path.lower().endswith((".ttf", ".ttc", ".otf")):
                font = _ttf.load(path)
        except Exception as exc:
            msg = "字体 %s 加载失败: %s" % (key, exc)
            if msg not in self.warnings:
                self.warnings.append(msg)
        self._font_cache[ck] = font
        return font

    def _text(self, cv: Canvas, op: dict, k: float) -> None:
        """渲染一段文字：字形轮廓 → 缩放 → 平移 → 填充。

        OFD 的 text op 里 x/y 是**基线起点**（mm），size 是字号（mm）。
        TrueType 的坐标是 font units、y 向上，所以要按 size/unitsPerEm 缩放，
        再把 y 取反（页面 y 向下）。
        """
        txt = op.get("text") or ""
        if not txt:
            return
        font = self._get_font(op.get("font") or "", op.get("font_id"),
                              mono=bool(op.get("fixed_width")))
        color = self._rgba(op.get("color"))
        if color is None:
            color = (0, 0, 0, 255)
        if font is None:
            msg = "缺字体 %s，文字未渲染" % (op.get("font") or "(未知)")
            if msg not in self.warnings:
                self.warnings.append(msg)
            return
        size = float(op.get("size") or 3.0) * k      # 字号换算成像素
        upem = float(font.units_per_em or 1000)
        sc = size / upem
        hscale = float(op.get("hscale") or 1.0)
        x0 = float(op.get("x") or 0.0) * k
        y0 = float(op.get("y") or 0.0) * k
        spacing = float(op.get("spacing") or 0.0) * k
        # 文字旋转（text.rot）：绕基线起点转，角度是度、顺时针为正（页面 y 向下）
        rot = float(op.get("rot") or 0.0)
        ca, sa = (math.cos(math.radians(rot)), math.sin(math.radians(rot))) \
            if rot else (1.0, 0.0)
        # 合成斜体：TrueType 没有专门的斜体轮廓，用剪切变换近似（约 12°）
        # 同理：字型自身已是斜体就不再剪切
        italic = bool(op.get("italic")) and not op.get("font_italic")
        shear = math.tan(math.radians(12.0)) if italic else 0.0
        # 合成粗体：把轮廓整体加粗，靠「描边 + 填充」实现
        # 表 45：Weight 100~900，默认 400。按档位决定加粗量，
        # 而不是只看 bold 布尔——这样 500/600/700 能区分出层次。
        weight = int(op.get("weight") or 400)
        # 表 44 的 Bold/Italic 说的是**字型文件本身**已经是粗体/斜体；
        # 表 45 的 Weight/Italic 说的是**这个文字对象**要不要粗/斜。
        # 字型本身已经粗了就不能再合成一遍，否则字形会糊成一团。
        if op.get("font_bold"):
            weight = 400
        if weight > 400:
            bw_ratio = {500: 0.012, 600: 0.022, 700: 0.035,
                        800: 0.046, 900: 0.058}.get(
                            min(900, int(round(weight / 100.0)) * 100), 0.035)
            bold_w = max(1.0, size * bw_ratio)
        else:
            bold_w = 0.0
        if op.get("bold") and not bold_w:
            bold_w = max(1.0, size * 0.035)

        # 11.4：CGTransform 的 Glyphs 是字形索引直给。内嵌子集字体常无 cmap，
        # 这时只能走索引；有 cmap 时优先用索引（它就是原文档用的那套字形）。
        glyphs = op.get("glyphs") or []
        one_glyph = op.get("glyph")
        cx = x0
        for idx, ch in enumerate(txt):
            if one_glyph is not None:
                gid = int(one_glyph)
            elif idx < len(glyphs):
                gid = int(glyphs[idx])
            else:
                gid = font.glyph_index(ch)
            adv = font.advance(gid) * sc * hscale
            if gid:
                polys = []
                for contour in font.glyph_outlines(gid):
                    pts = []
                    for px, py in contour:
                        gx = cx + px * sc * hscale + (shear * py * sc if shear else 0.0)
                        gy = y0 - py * sc
                        if rot:
                            dxp, dyp = gx - x0, gy - y0
                            gx = x0 + dxp * ca - dyp * sa
                            gy = y0 + dxp * sa + dyp * ca
                        pts.append((gx, gy))
                    if len(pts) >= 3:
                        polys.append(pts)
                if polys:
                    if bold_w:
                        # 加粗：沿轮廓描一圈再加填充（近似，不改变字形结构）
                        cv.fill_polygons(polys, color, "nonzero")
                        for poly in polys:
                            cv.stroke_polyline(poly, bold_w, color, "round",
                                               "round", 3.528, True)
                    else:
                        cv.fill_polygons(polys, color, "nonzero")
            cx += adv + spacing

    def _path(self, cv: Canvas, op: dict, k: float) -> None:
        subs = op.get("subs") or []
        if not subs:
            return
        raw_fill = op.get("fill")
        shd = raw_fill if isinstance(raw_fill, dict) else None
        fill = None if shd else self._rgba(raw_fill)
        stroke = self._rgba(op.get("stroke"))
        closed_flags = op.get("closed") or [False] * len(subs)
        rule = op.get("rule") or "nonzero"

        if fill is not None or shd is not None:
            polys = []
            for arr in subs:
                pts = [(arr[i] * k, arr[i + 1] * k)
                       for i in range(0, len(arr) - 1, 2)]
                if len(pts) >= 3:
                    polys.append(pts)
            if polys:
                if shd is not None and shd.get("kind") == "pattern":
                    # 8.3.3 底纹：平铺单元，不走逐像素插值
                    self._fill_pattern(cv, polys, shd, rule, k)
                elif shd is not None:
                    # scene 给的渐变几何是 **mm**，而着色时用的是像素坐标，
                    # 必须先把几何量乘 k 换算过去，否则色带位置全错。
                    sc2 = dict(shd)
                    sc2["start"] = [shd["start"][0] * k, shd["start"][1] * k]
                    sc2["end"] = [shd["end"][0] * k, shd["end"][1] * k]
                    if sc2.get("unit"):
                        sc2["unit"] = sc2["unit"] * k
                    if sc2.get("radii"):
                        sc2["radii"] = [r * k for r in sc2["radii"]]
                    if sc2.get("points"):
                        sc2["points"] = [(p0 * k, p1 * k, p2)
                                         for p0, p1, p2 in sc2["points"]]
                    self._fill_shading(cv, polys, sc2, rule)
                else:
                    cv.fill_polygons(polys, fill, rule)

        if stroke is not None:
            # 表 21 原文：线宽大于 0 时，无论多小都**最少绘制两个像素的宽度**；
            # 线宽为 0 时**绘制一个像素的宽度**。所以下限是按像素算的，
            # 不是 mm —— 同一个 0.05mm 在 72dpi 和 600dpi 下结果不同。
            w_mm = op.get("w")
            w_mm = 0.0 if w_mm is None else float(w_mm)
            # 表 21 的下限是「像素」：w=0 画 1 像素、w>0 最少 2 像素。
            # 但扫描线是隔一整行采样的（y+0.5），恰好 1 像素宽的多边形
            # 在部分 dpi 下只盖住半个采样点，实测 600dpi 会整条消失。
            # 所以实际给到 1.5 / 2.5 像素——比标准下限略宽，
            # 换来的是细线在任何 dpi 下都稳定可见。这是工程取舍，记在此处。
            if w_mm <= 0.0:
                w = 1.5 / k
            else:
                w = max(2.5 / k, w_mm)
            join = (op.get("join") or "miter").lower()
            cap = (op.get("cap") or "butt").lower()
            ml = float(op.get("miter") or 3.528)
            for i, arr in enumerate(subs):
                pts = [(arr[j] * k, arr[j + 1] * k)
                       for j in range(0, len(arr) - 1, 2)]
                if len(pts) < 2:
                    continue
                cl = closed_flags[i] if i < len(closed_flags) else False
                dash = op.get("dash") or []
                if dash:
                    # 8.2.4：虚线。切成实线段后逐段描边（拐角按 bevel）
                    pat = [float(d) * k for d in dash]
                    doff = float(op.get("dash_offset") or 0.0) * k
                    seq2 = pts + [pts[0]] if cl else pts
                    for piece in _split_dash(seq2, pat, doff):
                        if len(piece) >= 2:
                            cv.stroke_polyline(piece, w, stroke, "bevel",
                                               cap, ml, False)
                else:
                    cv.stroke_polyline(pts, w, stroke, join, cap, ml, cl)


def render_page(page, dpi: float = 150.0, ss: int = 4, for_print: bool = False):
    """与 render_pil.render_page 同签名，便于替换。返回 (Canvas, warnings)。

    ss 是抗锯齿的垂直子扫描线数：1 = 关闭（最快），4 = 默认，8 = 更平滑更慢。
    """
    from .scene import build_page_scene
    scene = build_page_scene(page, for_print=for_print).to_dict()
    r = SoftRenderer(dpi, ss)
    try:
        r.register_package(page.doc)
    except Exception:
        pass
    return r.render(scene), r.warnings


def render_to_png(scene, resources, dpi: float = 150.0, ss: int = 4):
    """与 render_pil.render_to_png 同签名：直接返回 PNG 字节。

    上层（ofdlite.__init__）用它做一次性导出，返回 bytes 而不是 Canvas。
    """
    r = SoftRenderer(dpi, ss)
    d = dict(scene) if isinstance(scene, dict) else scene.to_dict()
    if resources:
        d["resources"] = resources
    cv = r.render(d)
    return pngcodec.encode(cv.w, cv.h, bytes(cv.buf), 4)

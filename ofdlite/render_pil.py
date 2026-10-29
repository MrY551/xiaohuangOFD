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

from PIL import Image, ImageDraw, ImageFont

from . import font_manager
from .errors import RenderError
from .scene import PageScene

_DEFAULT_DPI = 150
# 超采样倍数：PIL 的 ImageDraw 不做抗锯齿，直接按输出分辨率画会得到
# 硬边线条和死黑文字（官方阅读器的实心笔画是抗锯齿的灰）。
# 做法是按 dpi × ss 画，最后 LANCZOS 缩回 dpi，成本是内存 ×ss²，画质接近原版。
_DEFAULT_SUPERSAMPLE = 3


def _dash_segments(pts, pattern):
    """把折线按 dash 模式切成若干实段。pattern 单位与 pts 相同（像素）。

    Pillow 的 draw.line 不支持虚线，只能自己按弧长切。pattern 是 [on, off, ...]
    循环；长度为奇数时应重复一遍（与 PostScript/SVG 行为一致）。
    """
    if not pattern or len(pts) < 2:
        return [pts]
    pat = list(pattern)
    if len(pat) % 2:
        pat = pat * 2
    pat = [max(0.5, float(v)) for v in pat]

    out, cur = [], [pts[0]]
    pi, remain = 0, pat[0]
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
            if pi % 2 == 0:          # 奇数下标=实段
                cur.append((px, py))
            remain -= step
            if remain <= 1e-9:
                if pi % 2 == 0 and len(cur) > 1:
                    out.append(cur)
                pi = (pi + 1) % len(pat)
                remain = pat[pi]
                cur = [(px, py)]
        if pi % 2 == 0:
            pass
    if pi % 2 == 0 and len(cur) > 1:
        out.append(cur)
    return out



def _px(mm: float, dpi: float) -> float:
    return mm * dpi / 25.4


class PilRenderer(object):
    """把 PageScene 画成 PIL 图像。"""

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
    def _font(self, family: str, size_px: float, bold: bool, italic: bool):
        key = (family, round(size_px, 1), bold, italic)
        cached = self._font_cache.get(key)
        if cached is not None:
            return cached
        path = font_manager.resolve(family, bold, italic)
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
        draw = ImageDraw.Draw(img, "RGBA")
        scale = self.work_dpi / 25.4

        for op in scene.ops:
            t = op.get("t")
            try:
                if t == "rect":
                    self._draw_rect(draw, op, scale)
                elif t == "path":
                    self._draw_path(draw, op, scale)
                elif t == "text":
                    self._draw_text(img, op, scale)
                elif t == "image":
                    self._draw_image(img, op, scale)
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
        fill = self._color(op.get("fill"))
        stroke = self._color(op.get("stroke"))
        width = max(1, int(round(op.get("w", 0.2) * scale)))
        subs = op.get("subs") or []
        closed_flags = op.get("closed") or [False] * len(subs)
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
                    # dash 在 scene 里是 mm，这里要换算成像素
                    scaled = [max(0.5, float(d) * scale) for d in dash]
                    for piece in _dash_segments(seq, scaled):
                        if len(piece) >= 2:
                            draw.line(piece, fill=stroke, width=width, joint="curve")
                else:
                    draw.line(seq, fill=stroke, width=width, joint="curve")
                if width > 2:   # 线粗时补端点，避免圆头缺失
                    r = width / 2.0
                    for px, py in (seq[0], seq[-1]):
                        draw.ellipse([px - r, py - r, px + r, py + r], fill=stroke)
        del scale

    def _draw_text(self, img: Image.Image, op: dict, scale: float) -> None:
        text = op.get("text") or ""
        if not text:
            return
        color = self._color(op.get("color")) or (0, 0, 0, 255)
        size_px = max(1.0, op.get("size", 3.0) * scale)
        font = self._font(op.get("font") or "", size_px, bool(op.get("bold")),
                          bool(op.get("italic")))
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

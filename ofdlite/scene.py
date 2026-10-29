# -*- coding: utf-8 -*-
"""场景生成：把页面模型翻译成与后端无关的绘制指令。

协议（JSON 友好，单位统一毫米，坐标原点在页面左上，Y 轴向下）：
    {"t": "save"}
    {"t": "restore"}
    {"t": "clip",   "subs": [[x,y,x,y,...], ...]}
    {"t": "path",   "subs": [[x,y,...]], "closed": [bool,...],
                    "fill": [r,g,b,a]|None, "stroke": [r,g,b,a]|None,
                    "w": 线宽mm, "dash": [mm,...]|None, "rule": "nonzero"|"evenodd"}
    {"t": "text",   "x","y","text","size","font","bold","italic",
                    "color":[r,g,b,a], "rot": 角度, "spacing": mm}
    {"t": "image",  "x","y","w","h", "res": 资源键, "mime": "image/png"}
    {"t": "rect",   "x","y","w","h", "fill":..., "stroke":...}   页面底色/选区

前端 Canvas2D 与 Qt QPainter 都按这 5 种指令实现，语义一致。
图片字节不塞进指令，另给 resources 字典（res 键 → base64），避免 JSON 膨胀。

复杂度：单页指令数 = 对象数 × 每个 TextCode，O(n)；不做任何全页光栅化。
"""
from __future__ import annotations

import base64
from typing import Dict, List, Optional
import math

from . import geometry as geo
from . import model
from . import xmlutil as X

PT_PER_MM = 72.0 / 25.4


def _rgba(color) -> Optional[List[float]]:
    if color is None:
        return None
    return [round(float(color[0]), 4), round(float(color[1]), 4),
            round(float(color[2]), 4), round(float(color[3]), 4)]


def _subs_to_flat(subs) -> (List[List[float]], List[bool]):
    flat = []
    closed = []
    for sp in subs:
        if len(sp.points) < 1:
            continue
        arr = []
        for x, y in sp.points:
            arr.append(round(x, 4))
            arr.append(round(y, 4))
        flat.append(arr)
        closed.append(bool(sp.closed))
    return flat, closed


class PageScene(object):
    """一页的绘制指令 + 用到的图片资源。"""

    def __init__(self, page: model.Page):
        self.page = page
        self.ops: List[dict] = []
        self.resources: Dict[str, dict] = {}
        self.size = page.size()
        self.warnings: List[str] = []

    def to_dict(self) -> dict:
        return {
            "page": self.page.id,
            "index": 0,
            "size": [round(self.size.w, 4), round(self.size.h, 4)],
            "rot": self.page.rotation(),
            "ops": self.ops,
            "resources": self.resources,
            "warnings": self.warnings,
        }


def build_page_scene(page: model.Page, include_background: bool = True) -> PageScene:
    sc = PageScene(page)
    size = sc.size
    # 页面自身的问题（Content.xml 缺失/不良构）先带出来，别让用户对着空白页猜
    sc.warnings.extend(getattr(page, "warnings", []) or [])
    if include_background:
        sc.ops.append({"t": "rect", "x": 0, "y": 0,
                       "w": round(size.w, 4), "h": round(size.h, 4),
                       "fill": [1, 1, 1, 1], "stroke": None})

    # 图层顺序：背景模板 → 本页图层 → 前景模板。
    # 用 all_layers() 而不是 page.layers —— 数电发票的框线、票头文字都在模板里。
    layers = page.all_layers() if hasattr(page, "all_layers") else page.layers
    for layer in layers:
        _emit_layer(sc, layer, geo.Matrix())
    if len(sc.ops) <= 1:
        if not sc.warnings:
            sc.warnings.append("页面没有可绘制对象")
    return sc


def _emit_layer(sc: PageScene, layer: model.Layer, base: geo.Matrix) -> None:
    lm = geo.Matrix.parse(layer.element.get("CTM"))
    m = base.mul(lm)
    for obj in layer.flat_objects():
        try:
            _emit_object(sc, obj, m)
        except Exception as exc:  # 单个对象坏了不影响整页
            sc.warnings.append("跳过对象 #%s: %s" % (obj.id, exc))


def _emit_object(sc: PageScene, obj: model.OFDObject, parent_m: geo.Matrix) -> None:
    # 表34：Visible 默认 true，显式 false 的对象不绘制
    if not X.bool_attr(obj.element.get("Visible"), True):
        return
    m = parent_m.mul(obj.ctm())
    alpha = obj.alpha()

    if isinstance(obj, model.TextObject):
        _emit_text(sc, obj, m, alpha)
    elif isinstance(obj, model.PathObject):
        _emit_path(sc, obj, m, alpha)
    elif isinstance(obj, model.ImageObject):
        _emit_image(sc, obj, m, alpha)
    elif isinstance(obj, model.CompositeObject):
        for ch in obj.children():
            _emit_object(sc, ch, m)


def _tc_pos(tc, last_x: float, last_y: float):
    """TextCode 的 X/Y 省略时沿用上一个（表46）。"""
    ex = tc.element.get("X")
    ey = tc.element.get("Y")
    x = X.fnum(ex, last_x) if ex is not None and str(ex).strip() != "" else last_x
    y = X.fnum(ey, last_y) if ey is not None and str(ey).strip() != "" else last_y
    return x, y


def _emit_text(sc: PageScene, obj: model.TextObject, m: geo.Matrix, alpha: float) -> None:
    text = obj.text()
    if not text and not obj.text_codes():
        return
    b = obj.boundary()
    size = obj.size_mm() * max(0.01, m.scale_y())
    fs = obj.font()
    family = fs.display_name() if fs else "Noto Sans CJK SC"
    if fs is None:
        sc.warnings.append("文本 #%s 引用了不存在的字体，已用默认字体" % obj.id)
    color = _rgba(obj.fill_color() if obj.has_fill() or True else (0, 0, 0, 1))
    if color is not None:
        color[3] = color[3] * alpha
    rot = m.rotation_deg()
    bold = obj.bold()
    italic = obj.italic()
    # 表45/表47：CharDirection 是单个字形的基线方向，ReadDirection 是排列方向，
    # 两者独立。CharDirection 直接叠到旋转角上。
    cdir = int(round(X.fnum(obj.element.get("CharDirection"), 0))) % 360
    rdir = int(round(X.fnum(obj.element.get("ReadDirection"), 0))) % 360
    hscale = X.fnum(obj.element.get("HScale"), 1.0)
    if hscale <= 0.01:
        hscale = 1.0
    rot_total = rot + cdir

    codes = obj.text_codes()
    if not codes:
        return
    # 单 TextCode 且无 DeltaX：整体绘制，后端一次 fillText，最快
    if len(codes) == 1 and not codes[0].delta_x and not codes[0].delta_y:
        tc = codes[0]
        tc_x, tc_y = _tc_pos(tc, 0.0, 0.0)
        px, py = m.apply(b.x + tc_x, b.y + tc_y)
        sc.ops.append({
            "t": "text", "id": obj.id, "x": round(px, 4), "y": round(py, 4),
            "text": tc.text, "size": round(size, 4), "font": family,
            "bold": bold, "italic": italic, "color": color,
            "rot": round(rot_total, 3),
            "hscale": round(hscale, 4),
            "spacing": round(obj.char_spacing() * m.scale_x(), 4),
        })
        return
    # 多 TextCode / 有字距：逐字符落的
    last_x = last_y = 0.0
    for tc in codes:
        tc_x, tc_y = _tc_pos(tc, last_x, last_y)
        last_x, last_y = tc_x, tc_y
        ddx = tc.delta_x
        ddy = tc.delta_y
        if not ddx and not ddy:
            px, py = m.apply(b.x + tc_x, b.y + tc_y)
            sc.ops.append({
                "t": "text", "id": obj.id, "x": round(px, 4), "y": round(py, 4),
                "text": tc.text, "size": round(size, 4), "font": family,
                "bold": bold, "italic": italic, "color": color,
                "rot": round(rot_total, 3), "hscale": round(hscale, 4), "spacing": 0.0,
            })
            continue
        adv_x = 0.0
        adv_y = 0.0
        text = tc.text
        em = obj.size_mm()
        for i, ch in enumerate(text):
            sx, sy = _dir_offsets(rdir, adv_x, adv_y)
            px, py = m.apply(b.x + tc_x + sx, b.y + tc_y + sy)
            sc.ops.append({
                "t": "text", "id": obj.id, "x": round(px, 4), "y": round(py, 4),
                "text": ch, "size": round(size, 4), "font": family,
                "bold": bold, "italic": italic, "color": color,
                "rot": round(rot_total, 3), "hscale": round(hscale, 4), "spacing": 0.0,
            })
            # DeltaX 是"后一个文字与前一个文字之间在 X 方向的偏移值"（表46），
            # 也就是相邻字形绘制点的距离，不是额外字距。
            # 数电发票里中文给 1em（3.175）、数字给 0.5em（1.587）、
            # Courier New 给 0.6em（2.539），都是字符自身宽度。
            # 只有远小于任何字符宽度（<0.2em）时才当成额外字距叠加。
            step_x = ddx[i] if i < len(ddx) else None
            if step_x is None or step_x <= 0:
                adv_x += _advance(ch, em)
            elif step_x < em * 0.2:
                adv_x += _advance(ch, em) + step_x
            else:
                adv_x += step_x
            if i < len(ddy):
                adv_y += ddy[i]


def _dir_offsets(read_dir: int, dx: float, dy: float):
    """ReadDirection 把水平排列旋到别的方向（表47）。

    ReadDirection：0 从左往右 / 90 从上往下 / 180 从右往左 / 270 从下往上。
    标准把它定义为"从 x 轴正方向顺时针到文字排列方向的角度"，
    所以水平方向的位移要按这个角度旋转。
    """
    if not read_dir:
        return dx, dy
    r = math.radians(read_dir)
    c, s = math.cos(r), math.sin(r)
    return dx * c - dy * s, dx * s + dy * c


def _advance(ch: str, em_mm: float) -> float:
    """字符前进宽度估计（mm）。用于 DeltaX 缺失时的兜底定位。"""
    if ord(ch) > 0x2E80:
        return em_mm
    if ch in "ilI.,:;'|!":
        return em_mm * 0.28
    if ch == " ":
        return em_mm * 0.25
    return em_mm * 0.52


def _emit_path(sc: PageScene, obj: model.PathObject, m: geo.Matrix, alpha: float) -> None:
    # Path 的 AbbreviatedData 坐标以 Boundary 左上角为原点（和 TextCode 的 X/Y 一致），
    # 不是对象坐标系绝对坐标。官方样例里 Boundary="56.7 96.1 1.2 21.6" 配
    # Data="M 0 0 L 0 21.6"，只有加上 Boundary 原点才落得对。
    subs = obj.subpaths(False)
    if not subs:
        return
    b = obj.boundary()
    flat, closed = [], []
    for sp in subs:
        if not sp.points:
            continue
        arr = []
        for x, y in sp.points:
            tx, ty = m.apply(b.x + x, b.y + y)
            arr.append(round(tx, 4))
            arr.append(round(ty, 4))
        flat.append(arr)
        closed.append(sp.closed)
    if not flat:
        return
    fill = _rgba(obj.fill_color()) if obj.has_fill() else None
    stroke = _rgba(obj.stroke_color()) if obj.has_stroke() else None
    if fill is not None:
        fill[3] *= alpha
    if stroke is not None:
        stroke[3] *= alpha
    w = obj.line_width() * ((m.scale_x() + m.scale_y()) / 2.0)
    dash = obj.dash_pattern()
    if dash:
        sc_ = (m.scale_x() + m.scale_y()) / 2.0
        dash = [round(d * sc_, 4) for d in dash]
    # Rule 的写法有 "EvenOdd"、"Even-Odd"、"even_odd" 几种，统一归一化
    rule_raw = (obj.element.get("Rule") or "").replace("-", "").replace("_", "").lower()
    rule = "evenodd" if rule_raw.startswith("even") else "nonzero"
    if fill is None and stroke is None:
        return
    sc.ops.append({
        "t": "path", "id": obj.id, "subs": flat, "closed": closed,
        "fill": fill, "stroke": stroke, "w": round(max(w, 0.05), 4),
        "dash": dash, "rule": rule,
    })


def _emit_image(sc: PageScene, obj: model.ImageObject, m: geo.Matrix, alpha: float) -> None:
    ctm = obj.ctm()
    b = obj.boundary().normalized()
    if b.is_empty():
        # 没有 Boundary 时退回用 CTM 的缩放当尺寸
        b = geo.Rect(0, 0, abs(ctm.a) or 1.0, abs(ctm.d) or 1.0)
    if b.is_empty():
        return
    got = obj.image_bytes()
    if got is None:
        sc.warnings.append("图片 #%s 资源缺失（ResourceID=%s）" % (obj.id, obj.resource_id()))
        return
    fname, data = got
    key = "img%d" % len(sc.resources)
    mime = _mime_of(fname, data)
    sc.resources[key] = {
        "mime": mime,
        "name": fname,
        "data": base64.b64encode(data).decode("ascii") if len(data) < 8 * 1024 * 1024 else "",
        "bytes": len(data),
    }
    # CTM 的语义分两种，必须区分：
    #   a) 纯缩放（b=c=0 且 a/d≠1）：它只是"图像单位 → 对象尺寸"的比例。
    #      数电发票写 CTM="30 0 0 20 0 0" 配 Boundary="90 8 30 20"，
    #      Boundary 已经给出落位与尺寸，再乘一次 CTM 会把图放大到页面外。
    #      此时 m = parent_m · ctm，右乘逆矩阵退回 parent_m 即可。
    #   b) 含旋转/斜切：四角按 CTM 变换才是对的。
    pure_scale = (abs(ctm.b) < 1e-6 and abs(ctm.c) < 1e-6 and
                  (abs(ctm.a - 1) > 1e-6 or abs(ctm.d - 1) > 1e-6))
    use_m = m.mul(ctm.invert()) if pure_scale else m
    corners = [use_m.apply(x, y) for x, y in b.corners()]
    sc.ops.append({
        "t": "image", "id": obj.id, "res": key,
        "corners": [[round(cx, 4), round(cy, 4)] for cx, cy in corners],
        "x": round(min(c[0] for c in corners), 4), "y": round(min(c[1] for c in corners), 4),
        "w": round(max(c[0] for c in corners) - min(c[0] for c in corners), 4),
        "h": round(max(c[1] for c in corners) - min(c[1] for c in corners), 4),
        "alpha": round(alpha, 4),
    })


def _mime_of(name: str, data: bytes) -> str:
    low = (name or "").lower()
    if data[:8] == b"\x89PNG\r\n\x1a\n" or low.endswith(".png"):
        return "image/png"
    if data[:2] == b"\xff\xd8" or low.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a") or low.endswith(".gif"):
        return "image/gif"
    if data[:2] == b"BM" or low.endswith(".bmp"):
        return "image/bmp"
    if low.endswith(".jb2") or low.endswith(".jbig2"):
        return "image/jbig2"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if b"<svg" in data[:200] or low.endswith(".svg"):
        return "image/svg+xml"
    return "application/octet-stream"


# ------------------------------------------------------------------ 选区盒

def selection_boxes(page: model.Page, objects, handle_mm: float = 0.0) -> List[dict]:
    """给前端画选中框 / 属性面板用：世界坐标四角 + 对象属性快照。"""
    out = []
    for obj in objects:
        b = obj.boundary().normalized()
        if b.is_empty():
            continue
        m = obj.world_matrix()
        corners = [m.apply(x, y) for x, y in b.corners()]
        item = {
            "id": obj.id,
            "type": obj.type_name,
            "corners": [[round(x, 4), round(y, 4)] for x, y in corners],
            "label": obj.describe(),
        }
        if isinstance(obj, model.TextObject):
            item["text"] = obj.text()
            item["size"] = round(obj.size_mm(), 3)
            item["bold"] = obj.bold()
            item["italic"] = obj.italic()
            item["font_id"] = obj.font_id()
            item["color"] = _rgba(obj.fill_color())
            fs = obj.font()
            item["font_name"] = fs.display_name() if fs else ""
        elif isinstance(obj, model.PathObject):
            item["fill"] = _rgba(obj.fill_color()) if obj.has_fill() else None
            item["stroke"] = _rgba(obj.stroke_color()) if obj.has_stroke() else None
            item["line_width"] = round(obj.line_width(), 3)
        elif isinstance(obj, model.ImageObject):
            got = obj.image_bytes()
            item["resource"] = got[0] if got else ""
        out.append(item)
    return out


def page_outline(page: model.Page) -> List[List[float]]:
    s = page.size()
    return [[0.0, 0.0], [round(s.w, 4), 0.0],
            [round(s.w, 4), round(s.h, 4)], [0.0, round(s.h, 4)]]

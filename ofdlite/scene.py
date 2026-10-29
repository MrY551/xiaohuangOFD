# -*- coding: utf-8 -*-
"""场景生成：把页面模型翻译成与后端无关的绘制指令。

协议（JSON 友好，单位统一毫米，坐标原点在页面左上，Y 轴向下）：
    {"t": "save"}
    {"t": "restore"}
    {"t": "clip",   "groups": [[[x,y,x,y,...], ...], ...]}
                     groups 外层每个元素是一个 Clip（**组间取交集**），
                     组内是若干子路径（**组内取并集**）。
                     坐标已换到页面 mm，原点页面左上。
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
from .stroke import stroke_to_polygon
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
        # 表 7：显示区域 = PhysicalBox ∩ ApplicationBox ∩ ContentBox。
        # 一般三者相同；有 ApplicationBox/ContentBox 时画布就是它们的交集，
        # 交集为空则整页空白。
        self.blank = False
        try:
            if hasattr(page, "show_box"):
                show, self.blank = page.show_box()
                # 有嵌套时用交集；纯 PhysicalBox 的情况保持 size（含它的偏移）
                if show is not None:
                    self.show = show
                else:
                    self.show = self.size
            else:
                self.show = self.size
        except Exception:
            self.show = self.size
        self.warnings: List[str] = []

    def to_dict(self) -> dict:
        return {
            "page": self.page.id,
            "index": 0,
            # 表 7：画布 = 显示区域（PhysicalBox ∩ ApplicationBox ∩ ContentBox）
            "size": [round(getattr(self.show, "w", self.size.w), 4),
                     round(getattr(self.show, "h", self.size.h), 4)],
            # 显示区域左上角就是画布原点，内容坐标要平移回去
            "origin": [round(getattr(self.show, "x", 0.0), 4),
                       round(getattr(self.show, "y", 0.0), 4)],
            "blank": bool(self.blank),
            "rot": self.page.rotation(),
            "ops": self.ops,
            "resources": self.resources,
            "warnings": self.warnings,
        }


def build_page_scene(page: model.Page, include_background: bool = True,
                     for_print: bool = False) -> PageScene:
    sc = PageScene(page)
    size = sc.size
    # 页面自身的问题（Content.xml 缺失/不良构）先带出来，别让用户对着空白页猜
    sc.warnings.extend(getattr(page, "warnings", []) or [])
    if include_background:
        _sw = getattr(sc.show, "w", size.w)
        _sh = getattr(sc.show, "h", size.h)
        # 背景矩形用**画布坐标** (0,0)：origin 的平移由渲染器的 _shift_ops 统一做，
        # 这里再写一遍 show.x/show.y 会被减两次，整页画到画布外。
        sc.ops.append({"t": "rect", "x": 0, "y": 0,
                       "w": round(_sw, 4), "h": round(_sh, 4),
                       "fill": [1, 1, 1, 1], "stroke": None})

    # 图层顺序：背景模板 → 本页图层 → 前景模板。
    # 用 all_layers() 而不是 page.layers —— 数电发票的框线、票头文字都在模板里。
    if hasattr(page, "all_layers"):
        try:
            layers = page.all_layers(for_print=for_print)
        except TypeError:
            layers = page.all_layers()
    else:
        layers = page.layers
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


def _clip_groups(groups, parent_m: geo.Matrix):
    """把裁剪区坐标变换到页面空间，返回可直接挂到指令上的结构。

    坐标处理：裁剪路径相对对象空间（外接矩形左上角），叠父矩阵即可；
    对象自身的 CTM **不参与**裁剪 —— 标准 8.1.4 说裁剪是对象空间内的设置，
    与外接矩形同一坐标系，这点和路径数据不同，别顺手乘上去。
    """
    out = []
    for item in groups:
        # 表 33：每组是 (Clip 自己的 CTM 或 None, 子路径列表)
        if isinstance(item, tuple) and len(item) == 2:
            ccm, subs = item
        else:
            ccm, subs = None, item
        # Clip 的 CTM 先作用在 Area 上（对象坐标系内），再叠父矩阵
        if ccm is not None:
            # 注意 Matrix.mul 的实际行为与它名字/注释描述的相反（实测：
            # parent(平移100).mul(ccm(缩放2)).apply(10,0) = 220，即"先 parent 后 ccm"）。
            # 这里要的是「先在对象坐标系内做 Clip 的 CTM，再映射到页面」，
            # 所以写成 ccm.mul(parent) —— 实测 apply(10,0)=120，正是 parent(ccm(p))。
            cm = geo.Matrix(*ccm).mul(parent_m)
        else:
            cm = parent_m
        g = []
        for arr in subs:
            pts = []
            for k in range(0, len(arr) - 1, 2):
                x, y = cm.apply(arr[k], arr[k + 1])
                pts.extend([round(x, 4), round(y, 4)])
            if len(pts) >= 4:
                g.append(pts)
        if g:
            out.append(g)
    return out


def _shading_desc(obj, attr: str):
    """把 FillColor/StrokeColor 里的渐变转成后端能用的描述。

    返回 None 表示不是渐变；否则形如
        {"kind": "axial", "segments": [[t, [r,g,b,a]], ...],
         "start": (x, y), "end": (x, y), "map": "direct",
         "unit": None|float, "extend": 0..3,
         "radii": (r0, r1), "ecc": float, "angle": float}
    坐标是**对象空间**（相对 Boundary 左上角，mm），后端按需乘缩放。

    8.3.4：渐变被当作颜色的一种，可以用于填充也可以用于勾边。
    """
    getter = getattr(obj, "fill_shading" if attr == "fill" else "stroke_shading", None)
    if getter is None:
        return None
    shd = getter()
    if shd is None:
        return None
    st, en = shd.points()
    d = {
        "kind": {"AxialShd": "axial", "RadialShd": "radial",
                 "GouraudShd": "gouraud", "LaGourandShd": "lattice",
                 "Pattern": "pattern"}.get(shd.kind, "axial"),
        "segments": [[p, list(c)] for p, c in shd.segments],
        "start": [st[0], st[1]],
        "end": [en[0], en[1]],
        "map": (shd.map_type() or "Direct").lower(),
        "unit": shd.map_unit(),
        "extend": shd.extend(),
    }
    if d["kind"] == "radial":
        r0, r1 = shd.radii()
        d["radii"] = [r0, r1]
        d["ecc"] = shd.eccentricity()
        d["angle"] = shd.angle()
    if d["kind"] == "pattern":
        # 8.3.3：底纹 = 一个图块（CellContent）按 XStep/YStep 平铺。
        # 图块里可含任意图元，所以要递归生成一份"小场景"的指令，
        # 后端先把它画成一张小图，再按步长平铺。
        attrs = shd.pattern_attrs()
        if attrs["w"] <= 0 or attrs["h"] <= 0:
            return None                     # Width/Height 必选，缺了没法平铺
        cell_ops = []
        page = getattr(obj, "page", None)
        for ch in shd.pattern_cell(page):
            sub = PageScene(page)
            _emit_object(sub, ch, geo.Matrix())
            cell_ops.extend(sub.ops)
        if not cell_ops:
            return None
        d["pattern"] = {
            "w": attrs["w"], "h": attrs["h"],
            "xstep": attrs["xstep"], "ystep": attrs["ystep"],
            "reflect": attrs["reflect"].lower(),
            "relative_to": attrs["relative_to"].lower(),
        }
        d["cell_ops"] = cell_ops

    if d["kind"] in ("gouraud", "lattice"):
        # 高洛德的坐标同样在对象空间，交给调用方一起过矩阵
        d["points"] = [(p["x"], p["y"], list(p["rgba"])) for p in shd.control_points()]
        d["back"] = list(shd.back_color()) if shd.back_color() else None
        d["vpr"] = shd.vertices_per_row()
        # 表 31：EdgeFlag 决定网格单元三角剖分的对角线方向
        try:
            d["edgeflag"] = shd.edge_flag()
        except Exception:
            d["edgeflag"] = None
        d["extend"] = shd.extend()
    return d


def _pick_map_mode(m, b, sample_pts):
    """决定对象坐标怎么映射到页面。

    两派写法都存在，实测都有真实文件在用：

      "std"   页面点 = CTM.apply(Boundary.xy + p)     ← 数电发票（政务系统）
      "bound" 页面点 = CTM.apply(p) + Boundary.xy     ← WPS 导出

    单位 CTM 时两者完全相同，无法区分也无所谓；CTM 带缩放/翻转时结果差很远
    （WPS 用 CTM="0.3528 0 0 -0.3528 0 297.0223" 做 y 翻转，按 std 会把
    f 平移吞掉，整页图形跑到页面外）。

    判据：Boundary 是「当前空间的外接矩形」（表 34），所以**正确映射后的
    包围盒应当落在 Boundary 附近**。两种都算一遍，取与 Boundary 重叠更大的。
    """
    if not sample_pts:
        return "std"
    if (abs(m.a - 1.0) < 1e-9 and abs(m.d - 1.0) < 1e-9 and
            abs(m.b) < 1e-9 and abs(m.c) < 1e-9 and
            abs(m.e) < 1e-9 and abs(m.f) < 1e-9):
        return "std"                     # 单位阵：两种一样，走老路

    def score(mode):
        xs, ys = [], []
        for x, y in sample_pts:
            tx, ty = m.apply(x, y)
            if mode == "bound":
                tx += b.x
                ty += b.y
            else:
                tx, ty = m.apply(b.x + x, b.y + y)
            xs.append(tx)
            ys.append(ty)
        if not xs:
            return -1e9
        # 包围盒与 Boundary 的相交面积（归一化到 Boundary 面积）
        ix0 = max(min(xs), b.x)
        iy0 = max(min(ys), b.y)
        ix1 = min(max(xs), b.x + b.w)
        iy1 = min(max(ys), b.y + b.h)
        inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        area = max(1e-9, b.w * b.h)
        # 超出 Boundary 太多要罚分（真正的外接矩形不该偏很远）
        bw = max(xs) - min(xs)
        bh = max(ys) - min(ys)
        over = max(0.0, bw - b.w) + max(0.0, bh - b.h)
        return inter / area - over / max(1e-9, b.w + b.h)

    return "bound" if score("bound") > score("std") else "std"


def _emit_object(sc: PageScene, obj: model.OFDObject, parent_m: geo.Matrix) -> None:
    # 表34：Visible 默认 true，显式 false 的对象不绘制
    if not X.bool_attr(obj.element.get("Visible"), True):
        return
    m = parent_m.mul(obj.ctm())
    alpha = obj.alpha()

    # 8.4 / 表34：裁剪区是**该图元对象**的属性，只作用于它自己，
    # 不是全局绘制状态。所以先发对象指令，再把这批指令挂上 clip。
    clips = obj.clips() if hasattr(obj, "clips") else []
    groups = _clip_groups(clips, parent_m) if clips else None

    start = len(sc.ops)
    if isinstance(obj, model.TextObject):
        _emit_text(sc, obj, m, alpha)
    elif isinstance(obj, model.PathObject):
        _emit_path(sc, obj, m, alpha)
    elif isinstance(obj, model.ImageObject):
        _emit_image(sc, obj, m, alpha)
    elif isinstance(obj, model.CompositeObject):
        # 第 13 章：复合对象的内容在 ResourceID 指向的矢量图像里，那些内容
        # 的坐标以**复合对象自己的外接矩形左上角**为原点（和所有对象空间一样）。
        # 所以要先平移 Boundary 原点，再叠复合对象自己的 CTM。
        b0 = obj.boundary().normalized()
        cm = m.mul(obj.ctm())
        shift = geo.Matrix(1.0, 0.0, 0.0, 1.0, b0.x, b0.y)
        inner = cm.mul(shift)
        # 表 50：矢量图像的 Width/Height 是裁剪边界，超出部分要裁掉
        vg = obj.vector_g() if hasattr(obj, "vector_g") else None
        clip_rect = None
        if vg:
            vw = float(vg.get("w") or 0.0)
            vh = float(vg.get("h") or 0.0)
            if vw > 0 and vh > 0:
                pts = [inner.apply(0, 0), inner.apply(vw, 0),
                       inner.apply(vw, vh), inner.apply(0, vh)]
                # clip 的结构是 [组[子路径[**扁平**坐标 x0,y0,x1,y1,...]]]，
                # 不是点对列表 —— 写错会让裁剪区失效或裁成空，整块画不出来。
                _flat = []
                for q in pts:
                    _flat.append(round(q[0], 4))
                    _flat.append(round(q[1], 4))
                clip_rect = [_flat]
        start = len(sc.ops)
        for ch in obj.children():
            _emit_object(sc, ch, inner)
        # TODO(表50 裁剪): Width/Height 的裁剪边界还没接对。
        # 实测挂上去会把内容整块裁空（clip 与 _clip_groups 的双层结构没对齐），
        # 与其裁错不如先不裁 —— 内容超出边界时多画一点，至少不会整块消失。
        # 记为未实现，等把 clip 结构再核一遍后补。
        if clip_rect:
            # 表 50：矢量图像的 Width/Height 是裁剪边界，超出部分要裁掉。
            # clip 的形状必须是 **三层** [组[子路径[扁平坐标]]]：
            #   clip_rect 已经是 [子路径] 这一层（clip_rect = [_flat]），
            #   外面还要再包一层「组」，因为组间是交集、组内是并集。
            # 少一层会让 _render_clipped 把 float 当子路径遍历（float has no len）。
            for it in sc.ops[start:]:
                it["clip"] = [clip_rect]

    # 14 章：动作附到该对象的每条指令上，界面据此画可点击热区。
    # 注意不能包在 if groups 里——没裁剪的对象同样可能有动作。
    # 表51 的 Region 是"该链接对象的启动区域"，属于图元内部数据：
    # 按 8.1.4，图元内部数据以 Boundary 左上角为原点，所以这里要抬到页面坐标，
    # 否则 Boundary 不在原点时热区会整体偏移（实测偏到左上角）。
    acts = _actions_in_page(obj, m, obj.boundary()) if hasattr(obj, "actions") else []
    for it in sc.ops[start:]:
        if groups:
            it["clip"] = groups
        if acts:
            it["actions"] = acts


def _actions_in_page(obj, m, b):
    """把动作里的 Region 从对象空间换到页面空间（表51 + 8.1.4）。"""
    out = []
    for act in obj.actions():
        act = dict(act)
        reg = act.get("region")
        if reg and reg.get("bbox"):
            x, y, w, h = reg["bbox"]
            pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            pp = [m.apply(b.x + px, b.y + py) for px, py in pts]
            xs = [p[0] for p in pp]
            ys = [p[1] for p in pp]
            act["region"] = {
                "bbox": [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)],
                "areas": reg.get("areas"),
            }
        out.append(act)
    return out


def _tc_pos(tc, last_x: float, last_y: float):
    """TextCode 的 X/Y 省略时沿用上一个（表46）。"""
    ex = tc.element.get("X")
    ey = tc.element.get("Y")
    x = X.fnum(ex, last_x) if ex is not None and str(ex).strip() != "" else last_x
    y = X.fnum(ey, last_y) if ey is not None and str(ey).strip() != "" else last_y
    return x, y


def _emit_text(sc: PageScene, obj: model.TextObject, m: geo.Matrix, alpha: float) -> None:
    # 11.4 字形变换：字符编码 → 字形索引。真正按字形渲染要解析字体的 CMap，
    # 需要 fontTools 与内嵌字体，本项目不做字形级绘制，按 Unicode 码点排版。
    # 一对一（字符数=字形数）视觉上没有差别；多对一/一对多/多对多会有差异，
    # 这种情况明确记一条告警，不假装支持。
    if hasattr(obj, "has_nonuniform_glyphs") and obj.has_nonuniform_glyphs():
        sc.warnings.append(
            "文本对象 #%s 有非均匀字形变换（多对一/一对多/多对多），"
            "当前按 Unicode 码点排版，连字与拆字效果可能与原作者渲染不同" % obj.id)
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
    # 11.4 / 表48：CGTransform 给「字符编码 → 字形索引」的映射。
    # 内嵌子集字体常常没有 cmap（WPS 导出的就是），这时必须靠 Glyphs 直接取字形。
    #
    # 三个字段都要按原文用：
    #   CodePosition  这段变换覆盖的字符起始位置（从 0 起），多个 CGTransform 时各不相同
    #   CodeCount     参与变换的**字符数**
    #   GlyphCount    变换后的**字形数**
    # CodeCount 与 GlyphCount 允许不等（一字符多字形、或多字符合一字形）。
    all_glyphs = []
    try:
        cgs = obj.cg_transforms()
        if cgs:
            total = 0
            for cg in cgs:
                total = max(total, cg["code_position"] + cg["code_count"])
            total = max(total, sum(len(c["glyphs"]) for c in cgs))
            buf = [None] * total
            for cg in cgs:
                cp = cg["code_position"]
                cc = max(1, cg["code_count"])
                gc = max(1, cg["glyph_count"])
                gl = cg.get("glyphs") or []
                if cc == gc:
                    for k in range(cc):
                        if k < len(gl) and cp + k < total:
                            buf[cp + k] = gl[k]
                else:
                    # 数量不等：按比例把字形分给字符（首个字符拿第一个字形）
                    for k in range(cc):
                        idx = int(k * gc / cc) if cc else 0
                        if idx < len(gl) and cp + k < total:
                            buf[cp + k] = gl[idx]
            all_glyphs = buf
    except Exception:
        all_glyphs = []
    # 单 TextCode 且无 DeltaX：整体绘制，后端一次 fillText，最快
    if len(codes) == 1 and not codes[0].delta_x and not codes[0].delta_y:
        tc = codes[0]
        tc_x, tc_y = _tc_pos(tc, 0.0, 0.0)
        px, py = m.apply(b.x + tc_x, b.y + tc_y)
        sc.ops.append({
            "t": "text", "id": obj.id, "x": round(px, 4), "y": round(py, 4),
            "text": tc.text, "size": round(size, 4), "font": family, "font_id": (fs.id if fs else None),
            "font_bold": bool(getattr(fs, "bold", False)) if fs else False,
            "font_italic": bool(getattr(fs, "italic", False)) if fs else False,
            "glyphs": all_glyphs or None,
            "bold": bold, "italic": italic, "weight": obj.weight(), "color": color,
            "rot": round(rot_total, 3),
            "hscale": round(hscale, 4),
            # 表44 FixedWidth：等宽字型每个字形都占满 em，定位别用字符类估计
            "fixed_width": bool(getattr(fs, "fixed_width", False)),
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
                "text": tc.text, "size": round(size, 4), "font": family, "font_id": (fs.id if fs else None),
            "font_bold": bool(getattr(fs, "bold", False)) if fs else False,
            "font_italic": bool(getattr(fs, "italic", False)) if fs else False,
                "bold": bold, "italic": italic, "weight": obj.weight(), "color": color,
                "rot": round(rot_total, 3), "hscale": round(hscale, 4), "spacing": 0.0,
                "fixed_width": bool(getattr(fs, "fixed_width", False)),
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
                "text": ch, "size": round(size, 4), "font": family, "font_id": (fs.id if fs else None),
            "font_bold": bool(getattr(fs, "bold", False)) if fs else False,
            "font_italic": bool(getattr(fs, "italic", False)) if fs else False,
                "glyph": (all_glyphs[i] if i < len(all_glyphs) else None),
                "font_bold": bool(getattr(fs, "bold", False)) if fs else False,
                "font_italic": bool(getattr(fs, "italic", False)) if fs else False,
                "bold": bold, "italic": italic, "weight": obj.weight(), "color": color,
                "rot": round(rot_total, 3), "hscale": round(hscale, 4), "spacing": 0.0,
                "fixed_width": bool(getattr(fs, "fixed_width", False)),
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
    """字符前进宽度估计（mm）。用于 DeltaX 缺失时的兜底定位。

    注：表44 的 FixedWidth 写的是"用于匹配替代字型"，是选字体的提示，
    不是"每字占满 em"的排版规则——这里不能拿它改前进量（见附三十九）。
    """
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
    # 先抽几个点判断这文件用哪派坐标写法（见 _pick_map_mode）
    sample = []
    for sp in subs:
        for pt in sp.points[:6]:
            sample.append(pt)
        if len(sample) >= 12:
            break
    mode = _pick_map_mode(m, b, sample)

    def _map(x, y):
        if mode == "bound":
            tx, ty = m.apply(x, y)
            return tx + b.x, ty + b.y
        return m.apply(b.x + x, b.y + y)

    flat, closed = [], []
    for sp in subs:
        if not sp.points:
            continue
        arr = []
        for x, y in sp.points:
            tx, ty = _map(x, y)
            arr.append(round(tx, 4))
            arr.append(round(ty, 4))
        flat.append(arr)
        closed.append(sp.closed)
    if not flat:
        return
    shd = _shading_desc(obj, "fill")
    if shd is not None:
        # 渐变坐标在对象空间，要和路径一样过矩阵，否则位置会错
        sx, sy = _map(shd["start"][0], shd["start"][1])
        ex, ey = _map(shd["end"][0], shd["end"][1])
        shd["start"] = [round(sx, 4), round(sy, 4)]
        shd["end"] = [round(ex, 4), round(ey, 4)]
        sc_ = (m.scale_x() + m.scale_y()) / 2.0
        if shd.get("unit"):
            shd["unit"] = round(shd["unit"] * sc_, 4)
        if "radii" in shd:
            shd["radii"] = [round(shd["radii"][0] * sc_, 4),
                            round(shd["radii"][1] * sc_, 4)]
        if shd.get("points"):
            mp = []
            for px, py, pc in shd["points"]:
                tx, ty = _map(px, py)
                mp.append((round(tx, 4), round(ty, 4),
                           [pc[0], pc[1], pc[2], pc[3] * alpha]))
            shd["points"] = mp
        if shd.get("segments"):
            # 透明度按对象 alpha 折算
            shd["segments"] = [[t, [c[0], c[1], c[2], c[3] * alpha]]
                               for t, c in shd["segments"]]
        fill = shd
    else:
        fill = _rgba(obj.fill_color()) if obj.has_fill() else None
    stroke = _rgba(obj.stroke_color()) if obj.has_stroke() else None
    if isinstance(fill, list) and fill is not None:
        fill[3] *= alpha
    if stroke is not None:
        stroke[3] *= alpha
    w = obj.line_width() * ((m.scale_x() + m.scale_y()) / 2.0)
    dash = obj.dash_pattern()
    dash_offset = 0.0
    if dash:
        sc_ = (m.scale_x() + m.scale_y()) / 2.0
        dash = [round(d * sc_, 4) for d in dash]
        # 8.2.4：DashOffset 只在 DashPattern 出现时有效
        dash_offset = round(obj.dash_offset() * sc_, 4)
    # Rule 的写法有 "EvenOdd"、"Even-Odd"、"even_odd" 几种，统一归一化
    rule_raw = (obj.element.get("Rule") or "").replace("-", "").replace("_", "").lower()
    rule = "evenodd" if rule_raw.startswith("even") else "nonzero"
    if fill is None and stroke is None:
        return
    # 8.2.3：MiterLimit 在三个后端都不可靠（Qt 的 setMiterLimit 实测不生效、
    # Canvas 也没可见延伸），所以实线 + Join=Miter 时自己按标准算描边多边形，
    # 让后端只做填充——这样截断一定按标准发生，三端也自然一致。
    # 虚线不走这条路（分段后算多边形代价高，且虚线拐角本来就少）。
    sw_fill, sw_stroke = fill, stroke
    if (stroke is not None and not dash
            and (obj.line_join() or "miter").lower() == "miter"):
        try:
            made = []
            for arr, cl in zip(flat, closed):
                pts = [(arr[i], arr[i + 1]) for i in range(0, len(arr) - 1, 2)]
                polys = stroke_to_polygon(
                    pts, float(w), "miter", (obj.line_cap() or "butt"),
                    float(obj.miter_limit()), closed=bool(cl))
                for poly in polys:
                    if len(poly) >= 3:
                        made.append([v for xy in poly for v in xy])
            if made:
                # 描边变成填充：三端都只做 fill，MiterLimit 一定生效
                sw_fill = stroke
                sw_stroke = None
                flat = made
                closed = [True] * len(made)
        except Exception as exc:
            sc.warnings.append("对象 #%s 描边转多边形失败: %s" % (obj.id, exc))

    sc.ops.append({
        "t": "path", "id": obj.id, "subs": flat, "closed": closed,
        "fill": sw_fill, "stroke": sw_stroke, # 表 21 的线宽下限由渲染器按"像素"处理（w>0 最少 2 像素、w=0 画 1 像素），
        # 这里必须原样传下去，不能提前抬成 0.05 —— 那会把 w=0 的语义吃掉。
        "w": round(max(w, 0.0), 4),
        "dash_offset": round(dash_offset, 4),
        # 8.2.2 / 8.2.5：线条连接样式与端点样式，取值 Miter/Round/Bevel、Butt/Round/Square
        "join": obj.line_join(), "cap": obj.line_cap(),
        # 8.2.3：只在 Join=Miter 时生效，默认 3.528（跟标准正文）
        "miter": round(obj.miter_limit(), 4),
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
    # 表 43：Substitution 是高分辨率输出用的替换图，有就优先它
    sub_id = ""
    try:
        sub_id = obj.substitution_id()
    except Exception:
        sub_id = ""
    if sub_id:
        sub_got = obj.image_bytes_of(sub_id)
        if sub_got is not None:
            got = sub_got

    fname, data = got
    key = "img%d" % len(sc.resources)
    mime = _mime_of(fname, data)
    sc.resources[key] = {
        "mime": mime,
        "name": fname,
        "data": base64.b64encode(data).decode("ascii") if len(data) < 8 * 1024 * 1024 else "",
        "bytes": len(data),
    }
    # 表 43：ImageMask 是与主图同大小的二值图，0 处透明
    mask_id = ""
    try:
        mask_id = obj.mask_id()
    except Exception:
        mask_id = ""
    mask_key = None
    if mask_id:
        mask_got = obj.image_bytes_of(mask_id)
        if mask_got is not None:
            mfname, mdata = mask_got
            mask_key = "img%d" % len(sc.resources)
            sc.resources[mask_key] = {
                "mime": _mime_of(mfname, mdata),
                "name": mfname,
                "data": base64.b64encode(mdata).decode("ascii") if len(mdata) < 8 * 1024 * 1024 else "",
                "bytes": len(mdata),
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

    # 表43：Border 是图像的子元素，线宽为 0 表示不画边框
    border = obj.image_border() if hasattr(obj, "image_border") else None
    if border:
        bd = dict(border)
        bd["w"] = round(bd["w"], 4)
        bd["radius_h"] = round(bd["radius_h"], 4)
        bd["radius_v"] = round(bd["radius_v"], 4)
        if bd["dash"]:
            bd["dash"] = [round(float(v), 4) for v in bd["dash"]]
            bd["dash_offset"] = round(float(bd["dash_offset"]), 4)
        else:
            bd["dash"] = None
            bd["dash_offset"] = 0.0
        bd["color"] = [bd["color"][0], bd["color"][1], bd["color"][2],
                       bd["color"][3] * alpha]

    sc.ops.append({
        "t": "image", "id": obj.id, "mask": mask_key, "res": key, "border": border,
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

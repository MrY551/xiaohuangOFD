# -*- coding: utf-8 -*-
"""几何：CTM 矩阵、Boundary 矩形、路径解析、命中测试。

OFD 的坐标单位是毫米，CTM 是 3x2 仿射矩阵，语义与 PDF 的 cm 一致：
    [ a b 0 ]
    [ c d 0 ]
    [ e f 1 ]
点变换： x' = a*x + c*y + e ,  y' = b*x + d*y + f
"""
from __future__ import annotations

import math
import re
from typing import List, Optional, Sequence, Tuple

from .xmlutil import bool_attr, fnum, fnum_list

Point = Tuple[float, float]


class Matrix(object):
    """3x2 仿射矩阵。不可变风格：所有运算返回新对象。"""

    __slots__ = ("a", "b", "c", "d", "e", "f")

    def __init__(self, a=1.0, b=0.0, c=0.0, d=1.0, e=0.0, f=0.0):
        self.a, self.b, self.c, self.d, self.e, self.f = (
            float(a), float(b), float(c), float(d), float(e), float(f))

    # ---------------------------------------------------------- 构造
    @classmethod
    def identity(cls) -> "Matrix":
        return cls()

    @classmethod
    def translate(cls, tx: float, ty: float) -> "Matrix":
        return cls(1, 0, 0, 1, tx, ty)

    @classmethod
    def scale(cls, sx: float, sy: Optional[float] = None) -> "Matrix":
        if sy is None:
            sy = sx
        return cls(sx, 0, 0, sy, 0, 0)

    @classmethod
    def rotate(cls, deg: float) -> "Matrix":
        r = math.radians(deg)
        cs, sn = math.cos(r), math.sin(r)
        return cls(cs, sn, -sn, cs, 0, 0)

    @classmethod
    def parse(cls, text, default: Optional["Matrix"] = None) -> "Matrix":
        """解析 "a b c d e f"。解析不出来时返回 default 或单位矩阵。"""
        if text is None:
            return default if default is not None else cls()
        if isinstance(text, Matrix):
            return text
        parts = [p for p in re.split(r"[,\s]+", str(text).strip()) if p]
        vals = []
        for p in parts:
            try:
                vals.append(float(p))
            except ValueError:
                vals.append(0.0)
        while len(vals) < 6:
            vals.append(1.0 if len(vals) in (0, 3) else 0.0)
        return cls(*vals[:6])

    # ---------------------------------------------------------- 运算
    def mul(self, other: "Matrix") -> "Matrix":
        """self * other：先做 other 再做 self（与 PDF cm 顺序一致）。"""
        a1, b1, c1, d1, e1, f1 = self.a, self.b, self.c, self.d, self.e, self.f
        a2, b2, c2, d2, e2, f2 = other.a, other.b, other.c, other.d, other.e, other.f
        return Matrix(
            a1 * a2 + b1 * c2,
            a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2,
            c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2,
            e1 * b2 + f1 * d2 + f2,
        )

    def apply(self, x: float, y: float) -> Point:
        return (self.a * x + self.c * y + self.e,
                self.b * x + self.d * y + self.f)

    def apply_vec(self, x: float, y: float) -> Point:
        """只做线性部分（不含平移），用于求方向/尺度。"""
        return (self.a * x + self.c * y, self.b * x + self.d * y)

    def determinant(self) -> float:
        return self.a * self.d - self.b * self.c

    def invert(self) -> "Matrix":
        det = self.determinant()
        if abs(det) < 1e-12:
            return Matrix()
        ia, ib = self.d / det, -self.b / det
        ic, id_ = -self.c / det, self.a / det
        ie = -(ia * self.e + ic * self.f)
        if_ = -(ib * self.e + id_ * self.f)
        return Matrix(ia, ib, ic, id_, ie, if_)

    def scale_x(self) -> float:
        """X 方向的长度缩放（用于字号）。"""
        return math.hypot(self.a, self.b)

    def scale_y(self) -> float:
        return math.hypot(self.c, self.d)

    def rotation_deg(self) -> float:
        """文字基线方向相对正右方的角度（度）。"""
        return math.degrees(math.atan2(self.b, self.a))

    def is_identity(self) -> bool:
        return (abs(self.a - 1) < 1e-9 and abs(self.b) < 1e-9 and
                abs(self.c) < 1e-9 and abs(self.d - 1) < 1e-9 and
                abs(self.e) < 1e-9 and abs(self.f) < 1e-9)

    def to_list(self) -> List[float]:
        return [self.a, self.b, self.c, self.d, self.e, self.f]

    def to_str(self) -> str:
        return " ".join(_fmt(v) for v in self.to_list())

    def __eq__(self, other):
        return isinstance(other, Matrix) and all(
            abs(x - y) < 1e-9 for x, y in zip(self.to_list(), other.to_list()))

    def __repr__(self):
        return "Matrix(%s)" % self.to_str()


def _fmt(v: float) -> str:
    """数值输出：去掉多余小数尾巴，OFD 里留 4 位足够。"""
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


class Rect(object):
    """轴对齐矩形，用左上 + 宽高。Boundary="x y w h"。"""

    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x=0.0, y=0.0, w=0.0, h=0.0):
        self.x, self.y, self.w, self.h = float(x), float(y), float(w), float(h)

    @classmethod
    def parse(cls, text, default: Optional["Rect"] = None) -> "Rect":
        if text is None:
            return default if default is not None else cls()
        if isinstance(text, Rect):
            return text
        parts = [p for p in re.split(r"[,\s]+", str(text).strip()) if p]
        vals = []
        for p in parts[:4]:
            try:
                vals.append(float(p))
            except ValueError:
                vals.append(0.0)
        while len(vals) < 4:
            vals.append(0.0)
        return cls(*vals)

    # ---------------------------------------------------------- 几何
    @property
    def x1(self) -> float:
        return self.x + self.w

    @property
    def y1(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    def normalized(self) -> "Rect":
        x0, y0 = min(self.x, self.x1), min(self.y, self.y1)
        return Rect(x0, y0, abs(self.w), abs(self.h))

    def union(self, other: "Rect") -> "Rect":
        a, b = self.normalized(), other.normalized()
        x0, y0 = min(a.x, b.x), min(a.y, b.y)
        x1, y1 = max(a.x1, b.x1), max(a.y1, b.y1)
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def contains(self, x: float, y: float, tol: float = 0.0) -> bool:
        r = self.normalized()
        return (r.x - tol <= x <= r.x1 + tol) and (r.y - tol <= y <= r.y1 + tol)

    def intersects_box(self, other: "Rect") -> bool:
        a, b = self.normalized(), other.normalized()
        return not (a.x1 < b.x or b.x1 < a.x or a.y1 < b.y or b.y1 < a.y)

    def corners(self) -> List[Point]:
        return [(self.x, self.y), (self.x1, self.y), (self.x1, self.y1), (self.x, self.y1)]

    def to_str(self) -> str:
        return " ".join(_fmt(v) for v in (self.x, self.y, self.w, self.h))

    def is_empty(self) -> bool:
        return abs(self.w) < 1e-9 or abs(self.h) < 1e-9

    def __repr__(self):
        return "Rect(%s)" % self.to_str()


class BBox(object):
    """浮点包围盒，累积用。"""

    __slots__ = ("x0", "y0", "x1", "y1", "_empty")

    def __init__(self):
        self._empty = True
        self.x0 = self.y0 = self.x1 = self.y1 = 0.0

    def add(self, x: float, y: float) -> None:
        if self._empty:
            self.x0 = self.x1 = x
            self.y0 = self.y1 = y
            self._empty = False
        else:
            if x < self.x0:
                self.x0 = x
            if x > self.x1:
                self.x1 = x
            if y < self.y0:
                self.y0 = y
            if y > self.y1:
                self.y1 = y

    def add_points(self, pts: Sequence[Point]) -> None:
        for x, y in pts:
            self.add(x, y)

    @property
    def empty(self) -> bool:
        return self._empty

    def rect(self) -> Rect:
        if self._empty:
            return Rect()
        return Rect(self.x0, self.y0, self.x1 - self.x0, self.y1 - self.y0)


# ------------------------------------------------------------------ 路径解析

class SubPath(object):
    """一条子路径：折线点 + 是否闭合。曲线已被压平成折线。"""
    __slots__ = ("points", "closed")

    def __init__(self, points: List[Point], closed: bool = False):
        self.points = points
        self.closed = closed

    def bbox(self) -> BBox:
        bb = BBox()
        bb.add_points(self.points)
        return bb

    def __repr__(self):
        return "SubPath(%d pts, closed=%s)" % (len(self.points), self.closed)


_CMD_RE = re.compile(r"[MmLlCcQqSsTtAaHhVvBbZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

# GB/T 33190-2016 表36《图形对象紧缩描述方式运算符》：
#   S  x y                          定义子绘制图形边线的起始点
#   M  x y                          当前点移动到指定点
#   L  x y                          从当前点连线
#   Q  x1 y1 x2 y2                  二次贝塞尔
#   B  x1 y1 x2 y2 x3 y3            三次贝塞尔（不是 C！）
#   A  rx ry angle large sweep x y  圆弧
#   C  无操作数                     SubPath 自动闭合（不是三次贝塞尔！）
# 标准里没有 T/H/V/Z，那是早期照 SVG 写的，只保留做容错。
# C 同时兼容"当三次贝塞尔用"的实现：按后面实际跟几个数字自适应。
_OP_ARG_COUNT = {
    "S": 2, "M": 2, "L": 2, "B": 6, "Q": 4, "A": 7, "C": 0,
    "T": 2, "H": 1, "V": 1, "Z": 0,   # 非标准，容错
}

# 弧转折线的每段采样数：64 段对 1mm 精度足够，打印 600dpi 下也看不出折线
_ARC_SEGMENTS = 64


def _arc_points(x0, y0, rx, ry, rot_deg, large_arc, sweep, x1, y1) -> List[Point]:
    """端点参数化椭圆弧 → 折线点（不含起点）。"""
    if rx == 0 or ry == 0:
        return [(x1, y1)]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(rot_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x0 - x1) / 2.0, (y0 - y1) / 2.0
    x1p = cos_p * dx2 + sin_p * dy2
    y1p = -sin_p * dx2 + cos_p * dy2
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = max(0.0, rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p)
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(num / den) if den > 1e-12 else 0.0
    if large_arc == sweep:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (x0 + x1) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (y0 + y1) / 2.0

    def ang(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        n = math.hypot(ux, uy) * math.hypot(vx, vy)
        if n < 1e-12:
            return 0.0
        a = math.acos(max(-1.0, min(1.0, dot / n)))
        return -a if (ux * vy - uy * vx) < 0 else a

    theta1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = ang((x1p - cxp) / rx, (y1p - cyp) / ry,
                 (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi

    pts = []
    steps = max(4, _ARC_SEGMENTS)
    for i in range(1, steps + 1):
        t = theta1 + dtheta * i / steps
        ex = rx * math.cos(t)
        ey = ry * math.sin(t)
        pts.append((cos_p * ex - sin_p * ey + cx, sin_p * ex + cos_p * ey + cy))
    return pts


def parse_path_data(data: str, ctm: Optional[Matrix] = None) -> List[SubPath]:
    """解析 PathObject/Path 的 AbbreviatedData。

    支持 M/L/C/Q/S/T/A/H/V/Z（大小写相对/绝对），以及 OFD 里额外出现的
    B（闭合曲线）当作 Z 处理。曲线压平成折线，折线精度见 _ARC_SEGMENTS。
    未知操作符整段跳过，不影响后续子路径。
    """
    if not data:
        return []
    tokens = _CMD_RE.findall(str(data))
    subs: List[SubPath] = []
    cur: List[Point] = []
    cx = cy = startx = starty = 0.0
    cmd = None
    i = 0
    n = len(tokens)

    def flush(closed=False):
        if len(cur) >= 2 or (len(cur) == 1 and closed):
            subs.append(SubPath(list(cur), closed))
        del cur[:]

    def num(k):
        try:
            return float(tokens[k])
        except (ValueError, IndexError):
            return 0.0

    while i < n:
        tok = tokens[i]
        if _is_cmd(tok):
            cmd = tok
            i += 1
            up0 = tok.upper()
            need = _OP_ARG_COUNT.get(up0, 0)
            if up0 in ("B", "C"):
                # 自适应：后面能凑出 6 个数字 ⇒ 三次贝塞尔；否则 ⇒ 闭合。
                # 标准写法是 B=cubic / C=close，但见过反着用的导出器。
                need = 6 if _count_numbers(tokens, i) >= 6 else 0
            if need == 0:
                # 闭合：C / Z
                if cur:
                    flush(True)
                cx, cy = startx, starty
                continue
            # 隐式重复：一个命令后跟多组参数
            while i + need <= n and not _is_cmd(tokens[i]):
                args = [num(i + k) for k in range(need)]
                i += need
                up = cmd.upper()
                rel = cmd.islower()
                # S = 定义子路径起始点（与 M 同类，但不把后续坐标当 L）
                if up in ("M", "S"):
                    px, py = (cx + args[0], cy + args[1]) if rel else (args[0], args[1])
                    flush(False)
                    cur.append((px, py))
                    cx, cy = px, py
                    startx, starty = px, py
                    if up == "M":
                        cmd = "l" if rel else "L"   # M 后续坐标按 L 处理
                elif up == "L":
                    px, py = (cx + args[0], cy + args[1]) if rel else (args[0], args[1])
                    if not cur:
                        cur.append((cx, cy))
                    cur.append((px, py))
                    cx, cy = px, py
                elif up == "H":
                    px = cx + args[0] if rel else args[0]
                    if not cur:
                        cur.append((cx, cy))
                    cur.append((px, cy))
                    cx = px
                elif up == "V":
                    py = cy + args[0] if rel else args[0]
                    if not cur:
                        cur.append((cx, cy))
                    cur.append((cx, py))
                    cy = py
                elif up in ("B", "C"):
                    p1 = (cx + args[0], cy + args[1]) if rel else (args[0], args[1])
                    p2 = (cx + args[2], cy + args[3]) if rel else (args[2], args[3])
                    p3 = (cx + args[4], cy + args[5]) if rel else (args[4], args[5])
                    if not cur:
                        cur.append((cx, cy))
                    cur.extend(_bezier(cx, cy, p1[0], p1[1], p2[0], p2[1], p3[0], p3[1]))
                    cx, cy = p3
                elif up in ("Q", "S"):
                    if up == "Q":
                        p1 = (cx + args[0], cy + args[1]) if rel else (args[0], args[1])
                        p2 = (cx + args[2], cy + args[3]) if rel else (args[2], args[3])
                    else:
                        # S/T 简化：用当前点做控制点
                        p1 = (cx, cy)
                        p2 = (cx + args[0], cy + args[1]) if rel else (args[0], args[1])
                    if not cur:
                        cur.append((cx, cy))
                    cur.extend(_quad(cx, cy, p1[0], p1[1], p2[0], p2[1]))
                    cx, cy = p2
                elif up == "T":
                    p2 = (cx + args[0], cy + args[1]) if rel else (args[0], args[1])
                    if not cur:
                        cur.append((cx, cy))
                    cur.append(p2)
                    cx, cy = p2
                elif up == "A":
                    ex, ey = (cx + args[5], cy + args[6]) if rel else (args[5], args[6])
                    if not cur:
                        cur.append((cx, cy))
                    cur.extend(_arc_points(cx, cy, args[0], args[1], args[2],
                                           bool(args[3]), bool(args[4]), ex, ey))
                    cx, cy = ex, ey
                if i < n and _is_cmd(tokens[i]):
                    break
        else:
            i += 1

    if cur:
        flush(False)

    if ctm is not None and not ctm.is_identity():
        out = []
        for sp in subs:
            out.append(SubPath([ctm.apply(x, y) for x, y in sp.points], sp.closed))
        return out
    return subs


def _is_cmd(tok: str) -> bool:
    return len(tok) == 1 and tok.isalpha()


def _count_numbers(tokens, i: int) -> int:
    """从 i 开始数连续的非命令 token 数量。"""
    n = 0
    while i + n < len(tokens) and not _is_cmd(tokens[i + n]):
        n += 1
    return n


def _bezier(x0, y0, x1, y1, x2, y2, x3, y3, steps: int = 24) -> List[Point]:
    pts = []
    for i in range(1, steps + 1):
        t = i / float(steps)
        mt = 1 - t
        a = mt * mt * mt
        b = 3 * mt * mt * t
        c = 3 * mt * t * t
        d = t * t * t
        pts.append((a * x0 + b * x1 + c * x2 + d * x3,
                    a * y0 + b * y1 + c * y2 + d * y3))
    return pts


def _quad(x0, y0, x1, y1, x2, y2, steps: int = 20) -> List[Point]:
    pts = []
    for i in range(1, steps + 1):
        t = i / float(steps)
        mt = 1 - t
        a = mt * mt
        b = 2 * mt * t
        c = t * t
        pts.append((a * x0 + b * x1 + c * x2, a * y0 + b * y1 + c * y2))
    return pts


# ------------------------------------------------------------------ 命中测试

def point_in_polygon(x: float, y: float, pts: Sequence[Point]) -> bool:
    """射线法。"""
    if len(pts) < 3:
        return False
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def point_near_polyline(x: float, y: float, pts: Sequence[Point], tol: float) -> bool:
    if len(pts) == 1:
        return math.hypot(pts[0][0] - x, pts[0][1] - y) <= tol
    for i in range(len(pts) - 1):
        if _dist_to_segment(x, y, pts[i], pts[i + 1]) <= tol:
            return True
    return False


def _dist_to_segment(px, py, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def bbox_of_subs(subs: Sequence[SubPath]) -> Rect:
    bb = BBox()
    for sp in subs:
        bb.add_points(sp.points)
    return bb.rect()


# ---------------------------------------------------------------- 9.3 非紧缩描述
#
# 表37：图形也可以用 XML 复杂类型描述，主要用于**区域（Region）**——
#       Area[@Start] 下一个或多个 Move / Line / QuadraticBezier /
#       CubicBezier / Arc / Close 依次连接。
# 9.3.1 原文：区域由一系列分路径(Area)组成，每个分路径都是闭合的。
#
# 这套写法和紧缩描述（表36 的 S/M/L/B/Q/A/C）是两条路，别混：
# 紧缩描述用在 PathObject/AbbreviatedData，非紧缩描述用在 Action/Region。

def _pos(text, default=(0.0, 0.0)):
    vals = fnum_list(text or "")
    if len(vals) >= 2:
        return (vals[0], vals[1])
    return default


def _arc_geometry(el, cx, cy):
    """表42 圆弧：EndPoint + EllipseSize + RotationAngle + LargeArc + SweepDirection。

    异常处理也照表42 抄：
      · 数组长度 > 2 只取前两个；长度 == 1 当作圆（该值是半径）；
      · 前两个里有一个为 0（或数组为空）→ 退化成一条直线；
      · 负值取绝对值；
      · RotationAngle 大于 360 按 360 取模。
    """
    end = _pos(el.get("EndPoint"), (cx, cy))
    size = fnum_list(el.get("EllipseSize") or "")
    if len(size) == 1:
        rx = ry = abs(size[0])
    elif len(size) >= 2:
        a, b = abs(size[0]), abs(size[1])
        # 较大的为长轴：长轴给 rx（水平半径），短轴给 ry
        rx, ry = (a, b) if a >= b else (b, a)
    else:
        rx = ry = 0.0
    if rx == 0 or ry == 0:
        return [end]
    rot = fnum(el.get("RotationAngle") or "0")
    rot = rot % 360.0
    large = bool_attr(el.get("LargeArc"), False)
    sweep = bool_attr(el.get("SweepDirection"), False)
    return _arc_points(cx, cy, rx, ry, rot, large, sweep, end[0], end[1])


def parse_region(region_el, ctm: Optional[Matrix] = None) -> List[SubPath]:
    """解析 CT_Region（表37-42，9.3 非紧缩描述）。

    每个 `<Area Start="x y">…</Area>` 是一条分路径；9.3.1 明确"每个分路径都是
    闭合的"，所以这里一律 closed=True（`Close` 元素只是显式写法，不改变结果）。
    """
    subs: List[SubPath] = []
    if region_el is None:
        return subs
    for area in region_el.iter():
        if _local(area.tag) != "Area":
            continue
        sp = parse_area(area)
        if sp is not None:
            subs.append(sp)
    if ctm is not None and not ctm.is_identity():
        return [SubPath([ctm.apply(x, y) for x, y in sp.points], sp.closed)
                for sp in subs]
    return subs


def parse_area(area, ctm: Optional[Matrix] = None) -> Optional[SubPath]:
    """把单个 Area（或等价元素）解析成一条闭合子路径。

    允许把 PathObject 直接当 Area 传进来：非标准写法里有人把
    Move/Line 写在 PathObject 下（标准只允许区域这么写），
    读得动比读不动强，但写回时仍按标准输出。
    """
    if area is None:
        return None
    cur = _pos(area.get("Start"), (0.0, 0.0))
    sub_start = cur
    pts: List[Point] = [cur]
    seen_cmd = False
    for ch in list(area):
        tag = _local(ch.tag)
        if tag == "Move":
            seen_cmd = True
            p = _pos(ch.get("Point1"), cur)
            if len(pts) > 1:
                # 同一 Area 里的第二段：按标准仍是同一个闭合分路径，
                # 这里保守地当作"跳点"，把前一段先收掉（不会丢几何）
                pass
            cur = p
            sub_start = p
            if not pts:
                pts = [p]
            continue
        elif tag == "Line":
            seen_cmd = True
            p = _pos(ch.get("Point1"), cur)
            pts.append(p)
            cur = p
        elif tag == "QuadraticBezier":
            seen_cmd = True
            p1 = _pos(ch.get("Point1"), cur)
            p2 = _pos(ch.get("Point2"), cur)
            pts.extend(_quad(cur[0], cur[1], p1[0], p1[1], p2[0], p2[1]))
            cur = p2
        elif tag == "CubicBezier":
            seen_cmd = True
            p3 = _pos(ch.get("Point3"), cur)
            p1 = _pos(ch.get("Point1"), cur)
            p2 = _pos(ch.get("Point2"), p3)
            pts.extend(_bezier(cur[0], cur[1], p1[0], p1[1], p2[0], p2[1], p3[0], p3[1]))
            cur = p3
        elif tag == "Arc":
            seen_cmd = True
            for pt in _arc_geometry(ch, cur[0], cur[1]):
                pts.append(pt)
                cur = pt
        elif tag == "Close":
            seen_cmd = True
            cur = sub_start
    if not seen_cmd or len(pts) < 2:
        return None
    if ctm is not None and not ctm.is_identity():
        pts = [ctm.apply(x, y) for x, y in pts]
    return SubPath(pts, True)


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


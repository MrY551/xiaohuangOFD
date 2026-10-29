# -*- coding: utf-8 -*-
"""把折线描边成多边形（自己算，不依赖渲染后端）。

为什么要自己算：8.2.3 的 MiterLimit 在三个后端都不可靠 ——
Qt 的 QPen::setMiterLimit 在 QImage 光栅路径上实测完全不生效（最小例子验证过），
Canvas 的 ctx.miterLimit 实测也没有可见延伸。既然后端给不了，就在生成绘制指令时
把「描边」算成「填充多边形」，三端只画填充，结果自然一致，而且 MiterLimit 一定生效。

对应标准：
  8.2.2 线条连接样式 Join = Miter / Round / Bevel
  8.2.3 线条连接点截断值 MiterLimit（Join=Miter 时，结合点长度 L 与线宽 W
        满足 sin(α/2) = W/L，即 L/W = 1/sin(α/2)；L/W 超过 MiterLimit 就截断成 Bevel）
  8.2.5 线条端点样式 Cap = Butt / Round / Square

几何约定与项目其它部分一致：单位 mm，y 向下，折线是 [(x, y), ...]。
"""
from __future__ import annotations

import math
from typing import List, Tuple, Optional

Point = Tuple[float, float]


def _sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def _add(a: Point, b: Point) -> Point:
    return (a[0] + b[0], a[1] + b[1])


def _mul(a: Point, k: float) -> Point:
    return (a[0] * k, a[1] * k)


def _len(a: Point) -> float:
    return math.hypot(a[0], a[1])


def _norm(a: Point) -> Point:
    n = _len(a)
    if n < 1e-12:
        return (0.0, 0.0)
    return (a[0] / n, a[1] / n)


def _dedup(pts: List[Point]) -> List[Point]:
    out = []
    for p in pts:
        if not out or _len(_sub(p, out[-1])) > 1e-9:
            out.append(p)
    return out


def _arc(center: Point, radius: float, a0: float, a1: float,
         steps: int = 8) -> List[Point]:
    """从角度 a0 到 a1 的圆弧（顺时针/逆时针按 a1-a0 的符号）。"""
    out = []
    for i in range(steps + 1):
        t = a0 + (a1 - a0) * i / float(steps)
        out.append((center[0] + radius * math.cos(t),
                    center[1] + radius * math.sin(t)))
    return out


def _join_miter(p0: Point, p1: Point, p2: Point, hw: float,
                miter_limit: float) -> Optional[List[Point]]:
    """在 p1 处按 Miter 连接，返回外侧的两个点（或 None 表示退化成 bevel）。

    d1、d2 是进出方向���单位向量）。外侧法线 n = -(d1+d2) 方向归一化。
    结合点长度 L = hw / sin(θ/2)，θ 是两段的内夹角；
    长度比 L/hw = 1/sin(θ/2) > MiterLimit 时按标准截断（改用 Bevel）。
    """
    d1 = _norm(_sub(p1, p0))
    d2 = _norm(_sub(p2, p1))
    if _len(d1) < 1e-12 or _len(d2) < 1e-12:
        return None
    cos_t = max(-1.0, min(1.0, d1[0] * d2[0] + d1[1] * d2[1]))
    theta = math.acos(cos_t)          # 两段方向的夹角（0=直行，π=折返）
    # 内侧夹角是 π - theta 时才是"尖角"；用 sin(theta/2) 计算 miter 比
    # 直行时 theta=0，miter 比为 1（无延伸）；折返时 theta=π，比为无穷大
    sin_half = math.sin(theta / 2.0)
    if sin_half < 1e-9:
        return None
    ratio = 1.0 / sin_half
    if miter_limit and ratio > miter_limit:
        return None                    # 超过截断值 → 退回 Bevel
    # 外侧方向：d1 - d2 指向折线的外侧（尖角那一侧）。
    # 早期写成 -d1 + d2 是内侧，算出来的 tip 会跑到折线里面去。
    bis = _norm(_sub(d1, d2))
    if _len(bis) < 1e-12:
        return None
    tip = _add(p1, _mul(bis, hw / sin_half))
    # 两侧的连接点（各段法线方向上的偏移点）
    n1 = (-d1[1], d1[0])
    n2 = (-d2[1], d2[0])
    if n1[0] * bis[0] + n1[1] * bis[1] < 0:   # 让法线朝外侧
        n1 = _mul(n1, -1.0)
    if n2[0] * bis[0] + n2[1] * bis[1] < 0:
        n2 = _mul(n2, -1.0)
    # 外侧是哪一侧由 bis 与第一段法线 n1 的点积符号决定：
    # 同向 → 外侧在 left，反向 → 在 right。固定加到 left 会让另一侧自交。
    outer_is_left = (n1[0] * bis[0] + n1[1] * bis[1]) > 0
    return (tip, outer_is_left)


def stroke_to_polygon(points: List[Point], width: float,
                      join: str = "miter", cap: str = "butt",
                      miter_limit: float = 3.528,
                      closed: bool = False,
                      arc_steps: int = 8) -> List[List[Point]]:
    """把折线描边成一个或两个闭合多边形（外轮廓 + 内轮廓，用 even-odd 填充）。

    返回 [[(x,y), ...], ...]；空列表表示线宽为 0 或点数不足，不必描边。

    复杂度 O(n)，n = 点数；每段两个偏移点，每个拐点最多 3 个点。
    """
    pts = _dedup(list(points))
    if closed and len(pts) > 2:
        pts = pts + [pts[0]]
    if len(pts) < 2 or width <= 0:
        return []
    hw = width / 2.0
    join = (join or "miter").lower()
    cap = (cap or "butt").lower()

    left: List[Point] = []
    right: List[Point] = []

    def seg_normals(a: Point, b: Point):
        d = _norm(_sub(b, a))
        return d, (-d[1], d[0])

    # 第一段
    d0, n0 = seg_normals(pts[0], pts[1])
    left.append(_add(pts[0], _mul(n0, hw)))
    right.append(_add(pts[0], _mul(n0, -hw)))

    for i in range(1, len(pts) - 1):
        p0, p1, p2 = pts[i - 1], pts[i], pts[i + 1]
        d1, n1 = seg_normals(p0, p1)
        d2, n2 = seg_normals(p1, p2)
        for side, sign in ((left, 1.0), (right, -1.0)):
            a = _add(p1, _mul(n1, hw * sign))
            b = _add(p1, _mul(n2, hw * sign))
            if join == "bevel":
                side.extend([a, b])
            elif join == "round":
                ang_a = math.atan2(a[1] - p1[1], a[0] - p1[0])
                ang_b = math.atan2(b[1] - p1[1], b[0] - p1[0])
                da = ang_b - ang_a
                while da > math.pi:
                    da -= 2 * math.pi
                while da < -math.pi:
                    da += 2 * math.pi
                side.extend(_arc(p1, hw, ang_a, ang_a + da, arc_steps))
            else:   # miter
                got = _join_miter(p0, p1, p2, hw, miter_limit)
                if got is None:
                    side.extend([a, b])          # 超过截断值 → Bevel
                else:
                    tip, outer_is_left = got
                    this_is_left = (sign > 0)
                    if this_is_left == outer_is_left:
                        side.extend([a, tip, b])
                    else:
                        side.extend([a, b])      # 内侧不延伸

    # 末段
    dn, nn = seg_normals(pts[-2], pts[-1])
    left.append(_add(pts[-1], _mul(nn, hw)))
    right.append(_add(pts[-1], _mul(nn, -hw)))

    # 端点样式（开放折线才处理；闭合折线首尾相接）
    if not closed:
        if cap == "square":
            left[0] = _add(left[0], _mul(d0, -hw))
            right[0] = _add(right[0], _mul(d0, -hw))
            left[-1] = _add(left[-1], _mul(dn, hw))
            right[-1] = _add(right[-1], _mul(dn, hw))
        elif cap == "round":
            c0 = pts[0]
            ang_r = math.atan2(right[0][1] - c0[1], right[0][0] - c0[0])
            ang_l = math.atan2(left[0][1] - c0[1], left[0][0] - c0[0])
            da = ang_l - ang_r
            while da > math.pi:
                da -= 2 * math.pi
            while da < -math.pi:
                da += 2 * math.pi
            left = _arc(c0, hw, ang_r, ang_r + da, arc_steps) + left
            c1 = pts[-1]
            ang_l2 = math.atan2(left[-1][1] - c1[1], left[-1][0] - c1[0])
            ang_r2 = math.atan2(right[-1][1] - c1[1], right[-1][0] - c1[0])
            da2 = ang_r2 - ang_l2
            while da2 > math.pi:
                da2 -= 2 * math.pi
            while da2 < -math.pi:
                da2 += 2 * math.pi
            right = right + _arc(c1, hw, ang_l2, ang_l2 + da2, arc_steps)

    # 外轮廓：左正向 + 右反向
    poly = left + list(reversed(right))
    if len(poly) < 3:
        return []
    return [poly]

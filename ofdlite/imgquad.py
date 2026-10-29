# -*- coding: utf-8 -*-
"""把图片按任意平行四边形落位（CTM 带旋转/斜切时）。

原来 `_image` 只用 x/y/w/h 画轴对齐矩形，遇到带旋转的 ImageObject 会画错。
OFD 的 ImageObject 用 CTM（仿射变换）定位，`Boundary` 只是未变换时的落位，
变换后的四个角就是 `scene` 里的 `corners`。

仿射变换把矩形映射成**平行四边形**，所以：
  任意目标点 P 可以写成 P = P0 + s·u + t·v   （u = P1-P0，v = P3-P0）
  解出 (s, t) 就得到源图里的归一化坐标，再乘源图尺寸采样。

这是 2×2 线性方程，直接算行列式求逆，不需要通用矩阵库。
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple


def quad_inverse(p0, u, v):
    """返回 ((a, b), (c, d)) 使 (s, t) = (a·dx + b·dy, c·dx + d·dy)。

    det = u.x·v.y − u.y·v.x；为 0 说明四边形退化成线，调用方要检查。
    """
    det = u[0] * v[1] - u[1] * v[0]
    if abs(det) < 1e-12:
        return None
    a = v[1] / det
    b = -v[0] / det
    c = -u[1] / det
    d = u[0] / det
    return (a, b, c, d)


def bbox_of(corners: Sequence[Sequence[float]], k: float,
            w: int, h: int) -> Optional[Tuple[int, int, int, int]]:
    """四角在画布上的整数包围盒（已裁到画布内）。"""
    xs = [p[0] * k for p in corners]
    ys = [p[1] * k for p in corners]
    x0 = max(0, int(min(xs)) - 1)
    y0 = max(0, int(min(ys)) - 1)
    x1 = min(w, int(max(xs)) + 2)
    y1 = min(h, int(max(ys)) + 2)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def sample_bilinear(px: bytes, sw: int, sh: int, sch: int,
                    fx: float, fy: float) -> Optional[Tuple[float, float, float, float]]:
    """在源图上按浮点坐标做双线性采样，返回 (r, g, b, a)（0~255 的 float）。"""
    if fx < 0.0 or fy < 0.0 or fx > sw - 1 or fy > sh - 1:
        return None
    x0 = int(fx)
    y0 = int(fy)
    wx = fx - x0
    wy = fy - y0
    x1 = x0 + 1 if x0 + 1 < sw else x0
    y1 = y0 + 1 if y0 + 1 < sh else y0
    r0 = y0 * sw * sch
    r1 = y1 * sw * sch
    i00 = r0 + x0 * sch
    i01 = r0 + x1 * sch
    i10 = r1 + x0 * sch
    i11 = r1 + x1 * sch
    w00 = (1 - wx) * (1 - wy)
    w01 = wx * (1 - wy)
    w10 = (1 - wx) * wy
    w11 = wx * wy
    if sch == 4:
        return (px[i00] * w00 + px[i01] * w01 + px[i10] * w10 + px[i11] * w11,
                px[i00 + 1] * w00 + px[i01 + 1] * w01 + px[i10 + 1] * w10 + px[i11 + 1] * w11,
                px[i00 + 2] * w00 + px[i01 + 2] * w01 + px[i10 + 2] * w10 + px[i11 + 2] * w11,
                px[i00 + 3] * w00 + px[i01 + 3] * w01 + px[i10 + 3] * w10 + px[i11 + 3] * w11)
    if sch == 3:
        return (px[i00] * w00 + px[i01] * w01 + px[i10] * w10 + px[i11] * w11,
                px[i00 + 1] * w00 + px[i01 + 1] * w01 + px[i10 + 1] * w10 + px[i11 + 1] * w11,
                px[i00 + 2] * w00 + px[i01 + 2] * w01 + px[i10 + 2] * w10 + px[i11 + 2] * w11,
                255.0)
    if sch == 2:
        a = px[i00 + 1] * w00 + px[i01 + 1] * w01 + px[i10 + 1] * w10 + px[i11 + 1] * w11
        g = px[i00] * w00 + px[i01] * w01 + px[i10] * w10 + px[i11] * w11
        return (g, g, g, a)
    g = px[i00] * w00 + px[i01] * w01 + px[i10] * w10 + px[i11] * w11
    return (g, g, g, 255.0)


def is_axis_aligned(corners: Sequence[Sequence[float]], tol: float = 0.01) -> bool:
    """四角是否构成轴对齐矩形（是的话走更快的老路径）。"""
    if len(corners) < 4:
        return False
    p0, p1, p2, p3 = corners[0], corners[1], corners[2], corners[3]
    return (abs(p0[1] - p1[1]) < tol and abs(p1[0] - p2[0]) < tol and
            abs(p2[1] - p3[1]) < tol and abs(p3[0] - p0[0]) < tol)

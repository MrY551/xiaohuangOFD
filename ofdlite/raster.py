# -*- coding: utf-8 -*-
"""自己写的多边形扫描线填充（替代 ImageDraw.polygon）。

为什么不用 ImageDraw.polygon：
- 它没有 fill-rule 参数，对**自交多边形**会挖出白色空洞；
- Miter 尖角描边出来的轮廓在尖角处必然自交（内侧有回折），
  用它会露出一个小白三角（实测）。

这里按 9.2 的填充规则实现两种语义：
  nonzero  同向环绕计数非零即填充（默认，对应 CT_GraphicUnit 的 Rule 缺省值）
  evenodd  交叉计数为奇数即填充（Rule="Even-Odd"）
两者在自交轮廓上结果不同，正是标准要求区分的。

算法是经典的「边表 + 活动边表」扫描线，复杂度 O(边数 × 扫描行数)，
对单个图形对象来说远小于整页像素数。不做抗锯齿——Pillow 后端本来就靠
3 倍超采样 + LANCZOS 降采样来获得平滑边缘，这里保持同一条链路。
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

Point = Tuple[float, float]


def _edges(polys: Sequence[Sequence[Point]], y_top: float, y_bot: float):
    """把所有多边形的边按 y 区间整理成 [(y_start, y_end, x_at_ystart, dx/dy, dir)]。"""
    out = []
    for poly in polys:
        n = len(poly)
        if n < 3:
            continue
        for i in range(n):
            x0, y0 = poly[i]
            x1, y1 = poly[(i + 1) % n]
            if abs(y1 - y0) < 1e-12:
                continue                     # 水平边不参与扫描（顶点规则靠半开区间处理）
            if y0 < y1:
                out.append((y0, y1, x0, (x1 - x0) / (y1 - y0), 1))
            else:
                out.append((y1, y0, x1, (x0 - x1) / (y0 - y1), -1))
    out.sort(key=lambda e: e[0])
    return out


def fill_mask(polys: Sequence[Sequence[Point]],
              width: int, height: int,
              rule: str = "nonzero") -> bytearray:
    """扫描线填充，返回 bytearray 掩码（每像素 1 字节，255 = 填充）。

    刻意不返回 PIL Image：本项目渲染链要能完全脱离 Pillow 运行。

    polys 里的坐标是**像素坐标**（已经乘过缩放），y 向下。
    用像素中心（y+0.5）采样，避免行边界上的重复计数。
    """
    mask = bytearray(width * height)
    if not polys or width <= 0 or height <= 0:
        return mask

    edges = _edges(polys, 0.0, float(height))
    if not edges:
        return mask

    rule = (rule or "nonzero").lower()
    even_odd = rule.startswith("even")

    idx = 0
    active = []          # [(x_at_current_y, dxdy, dir)]
    n_edges = len(edges)

    for y in range(height):
        yc = y + 0.5
        # 加入从本行开始生效的边（半开区间 [y_start, y_end)）
        while idx < n_edges and edges[idx][0] <= yc:
            e = edges[idx]
            if e[1] > yc:
                active.append([e[2] + (yc - e[0]) * e[3], e[3], e[4]])
            idx += 1
        # 丢掉已经结束的边
        if active:
            active = [a for a in active
                      if (a[0] is not None)]
        # 计算交点
        xs = []
        for a in active:
            xs.append((a[0], a[2]))
        # 活动边可能被上一行累积，这里重算本行 x
        # （简化实现：每行按边表重新推进，避免累积误差）
        xs = []
        for e in edges:
            if e[0] <= yc < e[1]:
                xs.append((e[2] + (yc - e[0]) * e[3], e[4]))
        if len(xs) < 2:
            continue
        xs.sort(key=lambda t: t[0])
        if even_odd:
            spans = []
            for i in range(0, len(xs) - 1, 2):
                spans.append((xs[i][0], xs[i + 1][0]))
        else:
            spans = []
            wind = 0
            start = None
            for x, d in xs:
                prev = wind
                wind += d
                if prev == 0 and wind != 0:
                    start = x
                elif prev != 0 and wind == 0 and start is not None:
                    spans.append((start, x))
                    start = None
            if start is not None and xs:
                spans.append((start, xs[-1][0]))
        base = y * width
        for x0, x1 in spans:
            if x1 < x0:
                x0, x1 = x1, x0
            a = int(round(x0))
            b = int(round(x1))
            if a < 0:
                a = 0
            if b > width:
                b = width
            for x in range(a, b):
                mask[base + x] = 255
    return mask

def fill_coverage(polys: Sequence[Sequence[Point]],
                  width: int, height: int,
                  rule: str = "nonzero", ss: int = 4) -> bytearray:
    """抗锯齿填充：返回每像素覆盖率（0~255），而不是 0/1 掩码。

    做法是「垂直 N 次子扫描线 + 水平精确重叠」：
      - 垂直方向抽 ss 条子扫描线（y + (k+0.5)/ss），把结果平均 —— 这是垂直抗锯齿
      - 水平方向不算采样点，而是算 span 与像素的**精确重叠长度** —— 水平抗锯齿
    两者相乘得到覆盖率，比纯 supersampling 省很多次求交。

    为什么不用 4x4 全超采样：求交是主要开销，垂直 ss 条已够；
    水平用解析重叠反而比采样更准。
    """
    cov = bytearray(width * height)
    if not polys or width <= 0 or height <= 0 or ss < 1:
        return cov
    edges = _edges(polys, 0.0, float(height))
    if not edges:
        return cov
    rule_l = (rule or "nonzero").lower()
    even_odd = rule_l.startswith("even")

    inv = 1.0 / ss
    # acc 整页复用：每行新建一个 width 长的 list 是纯开销
    # （A4@150dpi 要分配 217 万次），改成复用 + 只清零用过的区间。
    acc = [0.0] * width
    for y in range(height):
        lo = width
        hi = -1
        touched = False
        for k in range(ss):
            yc = y + (k + 0.5) * inv
            xs = []
            for e in edges:
                if e[0] <= yc < e[1]:
                    xs.append((e[2] + (yc - e[0]) * e[3], e[4]))
            if len(xs) < 2:
                continue
            xs.sort(key=lambda t: t[0])
            spans = []
            if even_odd:
                for i in range(0, len(xs) - 1, 2):
                    spans.append((xs[i][0], xs[i + 1][0]))
            else:
                wind = 0
                start = None
                for x, d in xs:
                    prev = wind
                    wind += d
                    if prev == 0 and wind != 0:
                        start = x
                    elif prev != 0 and wind == 0 and start is not None:
                        spans.append((start, x))
                        start = None
                if start is not None:
                    spans.append((start, xs[-1][0]))
            for x0, x1 in spans:
                if x1 <= x0:
                    continue
                if x0 < 0:
                    x0 = 0.0
                if x1 > width:
                    x1 = float(width)
                # 整像素区间直接累加，只有两个端点算小数重叠
                # （原来是逐像素算重叠，内循环太重，实测慢 4 倍）
                ix0 = int(x0)
                ix1 = int(x1)
                if ix0 < 0:
                    ix0 = 0
                if ix1 >= width:
                    ix1 = width - 1
                if ix1 < ix0:
                    continue
                if ix0 == ix1:
                    acc[ix0] += (x1 - x0) * inv
                    touched = True
                    if ix0 < lo:
                        lo = ix0
                    if ix0 > hi:
                        hi = ix0
                    continue
                # 左端点：从 x0 到 ix0+1
                acc[ix0] += ((ix0 + 1) - x0) * inv
                # 中间整像素
                for x in range(ix0 + 1, ix1):
                    acc[x] += inv
                # 右端点：从 ix1 到 x1
                acc[ix1] += (x1 - ix1) * inv
                touched = True
                if ix0 < lo:
                    lo = ix0
                if ix1 > hi:
                    hi = ix1
        if not touched:
            continue
        base = y * width
        for x in range(width):
            a = acc[x]
            if a <= 0.0:
                continue
            if a > 1.0:
                a = 1.0
            v = int(a * 255.0 + 0.5)
            if v:
                cov[base + x] = v
            acc[x] = 0.0
    return cov


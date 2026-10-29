# -*- coding: utf-8 -*-
"""两张发票渲染的像素级对齐比对。

    python tools/compare_render.py 官方截图.png 我的渲染.png

做法：两边都裁到页面区、归一到 10px/mm（2100×1400），然后
  1. 用暗像素的行/列投影找表格线，对比每条线的毫米坐标
  2. 逐段统计文字块的包围盒，对比位置与尺寸
输出毫米单位的差值表。
"""
from __future__ import annotations

import sys

import numpy as np
from PIL import Image

MM_W, MM_H = 210.0, 140.0
PPMM = 10.0
TARGET = (int(MM_W * PPMM), int(MM_H * PPMM))   # 2100 x 1400


def load_page(path):
    """载入图，裁出页面区，缩放到统一 ppmm。"""
    im = Image.open(path).convert("L")
    a = np.array(im)
    white_rows = (a > 245).mean(axis=1) > 0.5
    white_cols = (a > 245).mean(axis=0) > 0.5
    ys = np.where(white_rows)[0]
    xs = np.where(white_cols)[0]
    if len(ys) and len(xs):
        im = im.crop((int(xs.min()), int(ys.min()),
                      int(xs.max()) + 1, int(ys.max()) + 1))
    return im.resize(TARGET, Image.LANCZOS)


def dark_mask(im, thr=170):
    return (np.array(im) < thr)


def h_lines(mask, min_frac=0.45):
    """找贯穿整行的横线：每行暗像素占比超过阈值的连续行取中心。"""
    frac = mask.mean(axis=1)
    rows = frac > min_frac
    out = []
    i = 0
    n = len(rows)
    while i < n:
        if rows[i]:
            j = i
            while j + 1 < n and rows[j + 1]:
                j += 1
            out.append((i + j) / 2.0)
            i = j + 1
        else:
            i += 1
    return out


def v_lines(mask, y0_mm, y1_mm, min_frac=0.6):
    """在指定纵向范围内找竖线（避免文字干扰）。"""
    y0, y1 = int(y0_mm * PPMM), int(y1_mm * PPMM)
    sub = mask[y0:y1, :]
    frac = sub.mean(axis=0)
    cols = frac > min_frac
    out = []
    i = 0
    n = len(cols)
    while i < n:
        if cols[i]:
            j = i
            while j + 1 < n and cols[j + 1]:
                j += 1
            out.append((i + j) / 2.0)
            i = j + 1
        else:
            i += 1
    return out


def to_mm(v):
    return v / PPMM


def fmt(vals):
    return "[" + ", ".join("%.1f" % to_mm(v) for v in vals) + "]"


def main(argv):
    if len(argv) < 2:
        print("用法: compare_render.py 官方截图 我的渲染")
        return 2
    a = load_page(argv[0])
    b = load_page(argv[1])
    ma, mb = dark_mask(a), dark_mask(b)

    print("== 横线（全宽投影，占比 >45%）==")
    ha, hb = h_lines(ma), h_lines(mb)
    print("  官方 y(mm):", fmt(ha))
    print("  我的 y(mm):", fmt(hb))
    if len(ha) == len(hb):
        d = [to_mm(y2) - to_mm(y1) for y1, y2 in zip(ha, hb)]
        print("  差值(mm):  [%s]" % ", ".join("%+.1f" % x for x in d))
        print("  最大偏差: %.1f mm" % max(abs(x) for x in d))
    else:
        print("  ! 横线条数不同（%d vs %d），说明有线条没画出来或位置差太多" % (len(ha), len(hb)))

    print()
    print("== 竖线（表格区 30-110mm 投影，占比 >60%）==")
    va = v_lines(ma, 30, 110)
    vb = v_lines(mb, 30, 110)
    print("  官方 x(mm):", fmt(va))
    print("  我的 x(mm):", fmt(vb))
    if len(va) == len(vb):
        d = [to_mm(x2) - to_mm(x1) for x1, x2 in zip(va, vb)]
        print("  差值(mm):  [%s]" % ", ".join("%+.1f" % x for x in d))
        print("  最大偏差: %.1f mm" % max(abs(x) for x in d))
    else:
        print("  ! 竖线条数不同（%d vs %d）" % (len(va), len(vb)))

    print()
    print("== 文字块（按 8mm 高的横带统计暗像素包围盒）==")
    for y0 in range(0, 140, 8):
        ya0, ya1 = int(y0 * PPMM), int((y0 + 8) * PPMM)
        ra, rb = ma[ya0:ya1, :], mb[ya0:ya1, :]
        if ra.sum() < 200 and rb.sum() < 200:
            continue
        def bbox(m):
            ys, xs = np.where(m)
            if len(xs) == 0:
                return None
            return (to_mm(xs.min()), to_mm(xs.max()), to_mm(y0 + ys.min() / PPMM),
                    to_mm(y0 + ys.max() / PPMM))
        ba, bb = bbox(ra), bbox(rb)
        print("  y %3d-%3dmm  官方 pk=%-6d %s   我的 pk=%-6d %s" % (
            y0, y0 + 8, int(ra.sum()), ba, int(rb.sum()), bb))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

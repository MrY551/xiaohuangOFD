# -*- coding: utf-8 -*-
"""Cairo 后端可行性验证：只看这两件事能不能按标准做到。

1. 8.2.3 MiterLimit —— Qt 的 setMiterLimit 实测不生效、Canvas 也没可见延伸，
   这里验证 Cairo 的 cairo_set_miter_limit 是否能真正截断尖角。
2. 8.5 填充规则 —— nonzero（WINDING）/ evenodd 两种规则是否都对。

不写完整后端，只出两张对照图 + 逐像素判据。
用法：python3 tools/cairo_probe.py
"""
from __future__ import annotations

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import cairo
except ImportError:
    print("没装 pycairo：sudo apt-get install -y python3-cairo")
    sys.exit(2)

OUT = os.path.join(ROOT, "out")
DPI = 300.0
PX_PER_MM = DPI / 25.4


def mm(v: float) -> float:
    return v * PX_PER_MM


def new_surface(w_mm: float, h_mm: float):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                              int(round(mm(w_mm))), int(round(mm(h_mm))))
    cr = cairo.Context(surf)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    cr.set_source_rgb(0.08, 0.08, 0.08)
    # 先 scale，之后所有尺寸都用 mm 写；线宽如果先 mm() 再 scale 会被放大两次
    cr.scale(PX_PER_MM, PX_PER_MM)
    cr.set_line_width(2.5)
    return surf, cr


def sharp_path(cr, y_base: float):
    """25° 尖角：顶点 (30, y+4)，两端 (6, y+24) / (54, y+24)。"""
    cr.move_to(6, y_base + 24)
    cr.line_to(30, y_base + 4)
    cr.line_to(54, y_base + 24)


def probe_miter_limit():
    """三条同样形状的尖角，只改 MiterLimit。

    每条单独渲染到自己的 surface —— 放同一个 surface 上时，cairo 的
    miter_limit 虽然是图形状态，但三条叠在一张图里量起来容易串行。
    miter 比 = 1/sin(theta/2)：25° 尖角的方向向量夹角 79.6°，比值 1.562，
    所以 limit=1.0 必然截断、limit>=1.6 必然保留，1.5 落在边界附近。
    """
    cases = ((1.0, 5), (1.5, 32), (3.528, 59), (20, 86))
    surf, cr = new_surface(60, 115)
    tops = []
    for ml, base in cases:
        cr.save()
        cr.set_miter_limit(ml)
        cr.set_line_join(cairo.LINE_JOIN_MITER)
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        cr.new_path()
        sharp_path(cr, base)
        cr.stroke()
        cr.restore()

    path = os.path.join(OUT, "cairo_miter.png")
    surf.write_to_png(path)

    data = bytes(surf.get_data())
    stride = surf.get_stride()
    H = surf.get_height()
    cx = int(round(mm(30)))
    for ml, base in cases:
        found = None
        for py in range(max(0, int(round(mm(base - 6)))),
                        min(H, int(round(mm(base + 26))))):
            off_px = py * stride + cx * 4
            b, g, r = data[off_px], data[off_px + 1], data[off_px + 2]
            if r < 130 and g < 130 and b < 130:
                found = py
                break
        tops.append((ml, (found / PX_PER_MM - base) if found is not None else None))
    return path, tops


def probe_fill_rule():
    """外框 + 内框：内框同向 / 反向 × WINDING / EVEN_ODD。"""
    surf, cr = new_surface(80, 40)

    def box(x, y, w, h):
        cr.rectangle(x, y, w, h)

    results = []
    cases = [
        ("同向", False, cairo.FILL_RULE_WINDING),
        ("同向", False, cairo.FILL_RULE_EVEN_ODD),
        ("反向", True, cairo.FILL_RULE_WINDING),
        ("反向", True, cairo.FILL_RULE_EVEN_ODD),
    ]
    for i, (rev_name, reverse, rule) in enumerate(cases):
        x = 4 + i * 19
        cr.save()
        cr.translate(x, 4)
        cr.set_fill_rule(rule)
        cr.new_path()
        box(0, 0, 16, 32)                       # 外框
        # 内框：反方向时用逆时针画
        if reverse:
            cr.move_to(4, 8)
            cr.line_to(4, 24)
            cr.line_to(12, 24)
            cr.line_to(12, 8)
            cr.close_path()
        else:
            box(4, 8, 8, 16)
        cr.fill()
        cr.restore()
        # 读内框中心
        cx = int(round(mm(x + 8)))
        cy = int(round(mm(4 + 16)))
        results.append((rev_name, rule, cx, cy))
    path = os.path.join(OUT, "cairo_fillrule.png")
    surf.write_to_png(path)

    data = bytes(surf.get_data())
    stride = surf.get_stride()
    out = []
    for rev_name, rule, cx, cy in results:
        off = cy * stride + cx * 4
        b, g, r = data[off], data[off + 1], data[off + 2]
        out.append((rev_name, rule, (r, g, b)))
    return path, out


def main():
    os.makedirs(OUT, exist_ok=True)
    print("pycairo %s / libcairo %s" % (cairo.version, cairo.cairo_version_string()))
    print()

    p1, rows = probe_miter_limit()
    print("=== 8.2.3 MiterLimit（25° 尖角，miter 比 1.562）===")
    print("  理论：limit=1.0 必然截断（更钝）、limit>=1.6 必然保留（更尖）")
    bad = 0
    for ml, off in rows:
        print("  MiterLimit=%-6s 尖端相对基线偏移 = %s mm" % (
            ml, "%.2f" % off if off is not None else "未测到"))
    vals = [off for _, off in rows]
    if None in vals:
        print("  结果: 失败（有线条没测到墨迹）")
        bad += 1
    elif abs(vals[0] - vals[2]) < 0.10:
        print("  结果: 失败（limit=1.0 与 3.528 一样，MiterLimit 没生效）")
        bad += 1
    elif vals[0] > vals[2] + 0.10 and abs(vals[2] - vals[3]) < 0.10:
        print("  结果: 通过 —— MiterLimit 真正生效（1.0 被截断，3.528 与 20 一致）")
    else:
        print("  结果: 存疑（有差异但规律不对）")
        bad += 1
    print("  图: %s" % os.path.relpath(p1, ROOT))
    print()

    p2, out = probe_fill_rule()
    print("=== 8.5 填充规则（外框+内框）===")
    print("  理论：同向 WINDING 中心填、同向 EVEN_ODD 空、反向 WINDING 空、反向 EVEN_ODD 空")
    expect = [True, False, False, False]
    for (rev_name, rule, rgb), want in zip(out, expect):
        filled = rgb[0] < 130
        rule_name = "WINDING" if rule == cairo.FILL_RULE_WINDING else "EVEN_ODD"
        ok = (filled == want)
        print("  内框%-4s %-9s 中心=%-16s %s" % (
            rev_name, rule_name, str(rgb), "ok" if ok else "FAIL"))
        if not ok:
            bad += 1
    print("  图: %s" % os.path.relpath(p2, ROOT))
    print()
    print("总体: %s" % ("全部通过" if bad == 0 else "失败 %d 项" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

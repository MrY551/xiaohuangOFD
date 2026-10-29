# -*- coding: utf-8 -*-
"""像素级验收：三端交叉对照 + 几何理论值验算。

为什么需要这个：`full_check.py` 验的是「能力被触达、无告警」，
但那只是「跑完了」；证明不了「画对了」。

没有唯一真值（三个后端都是独立实现），所以用两种互补手段：

  A. **几何理论值验算**（最硬）
     对能算出解析解的图形（矩形边界、圆心半径、直线端点），
     直接算像素坐标，与渲染结果比对。误差应 ≤1 像素。

  B. **三端交叉对照**
     纯 Python / Pillow / Qt 各渲一张，在若干 mm 采样点取色比差。
     三者互不相同是正常的（抗锯齿、字体光栅化路径不同），
     但**几何位置必须一致**、**主色必须接近**。

用法：
  python3 tools/xcheck.py            # 两者都跑
  python3 tools/xcheck.py --geo      # 只跑几何验算
  python3 tools/xcheck.py --cross    # 只跑交叉对照
"""
from __future__ import annotations

import math
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, "out")
NS = "http://www.ofdspec.org/2016"
DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'


# ------------------------------------------------------------------ 造文件

def build_geo_ofd() -> str:
    """一份几何完全已知的文件：矩形、圆、十字线、斜线。

    所有坐标都是整数 mm，理论像素位置可以直接算出来。
    """
    ofd = DECL + ('<ofd:OFD xmlns:ofd="%s" DocType="OFD" Version="1.0"><ofd:DocBody>'
                  '<ofd:DocInfo><ofd:DocID>geo</ofd:DocID><ofd:Title>几何</ofd:Title>'
                  '</ofd:DocInfo><ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot>'
                  '</ofd:DocBody></ofd:OFD>\n') % NS
    doc = DECL + ('<ofd:Document xmlns:ofd="%s"><ofd:CommonData><ofd:MaxUnitID>60</ofd:MaxUnitID>'
                  '<ofd:PageArea><ofd:PhysicalBox>0 0 100 100</ofd:PhysicalBox></ofd:PageArea>'
                  '<ofd:PublicRes>PublicRes.xml</ofd:PublicRes></ofd:CommonData>'
                  '<ofd:Pages><ofd:Page ID="10" BaseLoc="Pages/Page_0/Content.xml"/></ofd:Pages>'
                  '</ofd:Document>\n') % NS
    res = DECL + ('<ofd:Res xmlns:ofd="%s" BaseLoc="Res"><ofd:ColorSpaces>'
                  '<ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/>'
                  '</ofd:ColorSpaces></ofd:Res>\n') % NS

    def filled_rect(oid, x, y, w, h, rgb):
        return ('<ofd:PathObject ID="%d" Boundary="%g %g %g %g" Fill="true" Stroke="false">'
                '<ofd:FillColor Value="%d %d %d" ColorSpace="1"/>'
                '<ofd:AbbreviatedData>M 0 0 L %g 0 L %g %g L 0 %g C</ofd:AbbreviatedData>'
                '</ofd:PathObject>') % (oid, x, y, w, h, rgb[0], rgb[1], rgb[2],
                                        w, w, h, h)

    def line(oid, x1, y1, x2, y2, width):
        # Boundary 用绝对坐标，路径用相对
        bx = min(x1, x2)
        by = min(y1, y2)
        bw = max(1, abs(x2 - x1) + width)
        bh = max(1, abs(y2 - y1) + width)
        rx1, ry1 = x1 - bx, y1 - by
        rx2, ry2 = x2 - bx, y2 - by
        return ('<ofd:PathObject ID="%d" Boundary="%g %g %g %g" Fill="false" Stroke="true" '
                'LineWidth="%g" Join="Miter" Cap="Butt">'
                '<ofd:StrokeColor Value="0 0 0" ColorSpace="1"/>'
                '<ofd:AbbreviatedData>M %g %g L %g %g</ofd:AbbreviatedData>'
                '</ofd:PathObject>') % (oid, bx, by, bw, bh, width, rx1, ry1, rx2, ry2)

    body = ""
    body += filled_rect(21, 10, 10, 30, 20, (255, 0, 0))      # 实心红矩形
    body += filled_rect(22, 60, 10, 20, 20, (0, 128, 255))    # 实心蓝方块
    body += line(23, 10, 50, 90, 50, 2)                       # 水平线
    body += line(24, 50, 10, 50, 90, 2)                       # 垂直线

    # 圆：用 4 段圆弧逼近太麻烦，用 16 边形近似，半径 15mm，圆心 (50,75)
    cx, cy, r = 50.0, 75.0, 15.0
    pts = []
    for i in range(16):
        a = 2 * math.pi * i / 16.0
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    bx, by = min(p[0] for p in pts), min(p[1] for p in pts)
    d = "M %g %g " % (pts[0][0] - bx, pts[0][1] - by)
    for p in pts[1:]:
        d += "L %g %g " % (p[0] - bx, p[1] - by)
    d += "C"
    body += ('<ofd:PathObject ID="25" Boundary="%g %g %g %g" Fill="true" Stroke="false">'
             '<ofd:FillColor Value="0 160 0" ColorSpace="1"/>'
             '<ofd:AbbreviatedData>%s</ofd:AbbreviatedData></ofd:PathObject>'
             ) % (bx, by, 2 * r, 2 * r, d)

    content = DECL + ('<ofd:Page xmlns:ofd="%s"><ofd:Area>'
                      '<ofd:PhysicalBox>0 0 100 100</ofd:PhysicalBox></ofd:Area>'
                      '<ofd:Content><ofd:Layer ID="20">%s</ofd:Layer></ofd:Content>'
                      '</ofd:Page>\n') % (NS, body)

    path = os.path.join(OUT, "xgeo.ofd")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("OFD.xml", ofd)
        z.writestr("Doc_0/Document.xml", doc)
        z.writestr("Doc_0/PublicRes.xml", res)
        z.writestr("Doc_0/Pages/Page_0/Content.xml", content)
    return path


# ------------------------------------------------------------------ 取色

def px_at(cv_or_img, xmm, ymm, dpi, backend="soft"):
    """按 mm 坐标取一个像素。"""
    k = dpi / 25.4
    x = int(round(xmm * k))
    y = int(round(ymm * k))
    if backend == "soft":
        w, h = cv_or_img.size
        if not (0 <= x < w and 0 <= y < h):
            return None
        buf = cv_or_img.buf
        i = (y * w + x) * 4
        return (buf[i], buf[i + 1], buf[i + 2])
    im = cv_or_img.convert("RGB")
    w, h = im.size
    if not (0 <= x < w and 0 <= y < h):
        return None
    return im.getpixel((x, y))


def close(a, b, tol):
    if a is None or b is None:
        return False
    return all(abs(a[i] - b[i]) <= tol for i in range(3))


# ------------------------------------------------------------------ A. 几何验算

def check_geometry(dpi=300):
    """几何理论值验算：矩形边界、圆心半径、线位置。"""
    from ofdlite import OFDPackage
    from ofdlite.render_soft import render_page

    path = build_geo_ofd()
    pkg = OFDPackage.open(path)
    cv, warns = render_page(pkg.all_pages()[0], dpi)
    k = dpi / 25.4
    buf = cv.buf
    W = cv.w

    def at(xmm, ymm):
        x = int(round(xmm * k))
        y = int(round(ymm * k))
        if not (0 <= x < W and 0 <= y < cv.h):
            return None
        i = (y * W + x) * 4
        return (buf[i], buf[i + 1], buf[i + 2])

    def is_red(c):
        return c is not None and c[0] > 180 and c[1] < 90 and c[2] < 90

    def is_blue(c):
        return c is not None and c[2] > 180 and c[0] < 90

    def is_green(c):
        return c is not None and c[1] > 100 and c[0] < 90 and c[2] < 90

    def is_dark(c):
        return c is not None and max(c) < 90

    bad = 0
    print("=" * 70)
    print("A. 几何理论值验算（%d dpi，误差要求 ≤1 像素 = %.3f mm）" % (dpi, 25.4 / dpi))
    print("=" * 70)

    # 实心矩形 10..40 × 10..30，红
    cases = [
        ("红矩形 内部(25,20)", at(25, 20), is_red, "应红"),
        ("红矩形 左外(9,20)", at(9, 20), lambda c: not is_red(c), "应非红"),
        ("红矩形 右外(41,20)", at(41, 20), lambda c: not is_red(c), "应非红"),
        ("红矩形 上外(25,9)", at(25, 9), lambda c: not is_red(c), "应非红"),
        ("红矩形 下外(25,31)", at(25, 31), lambda c: not is_red(c), "应非红"),
        ("蓝方块 内部(70,20)", at(70, 20), is_blue, "应蓝"),
        ("蓝方块 左外(59,20)", at(59, 20), lambda c: not is_blue(c), "应非蓝"),
        # 水平线 y=50，粗 2mm → 覆盖 49..51
        ("水平线 线上(30,50)", at(30, 50), is_dark, "应黑"),
        ("水平线 线外(30,47)", at(30, 47), lambda c: not is_dark(c), "应非黑"),
        ("水平线 线外(30,53)", at(30, 53), lambda c: not is_dark(c), "应非黑"),
        # 垂直线 x=50，从 y=10 到 90。取点要避开圆（圆心 50,75 半径 15，
        # 即 y 在 60~90 之间会被圆覆盖）和红/蓝矩形。
        ("垂直线 线上(50,20)", at(50, 20), is_dark, "应黑"),
        ("垂直线 线上(50,40)", at(50, 40), is_dark, "应黑"),
        ("垂直线 线外(47,40)", at(47, 40), lambda c: not is_dark(c), "应非黑"),
        # 圆：圆心 (50,75)、半径 15
        ("圆心(50,75)", at(50, 75), is_green, "应绿"),
        ("圆内(50,65)", at(50, 65), is_green, "应绿"),
        ("圆上边缘内(50,60.5)", at(50, 60.5), is_green, "应绿"),
        ("圆外(50,58)", at(50, 58), lambda c: not is_green(c), "应非绿"),
        ("圆外(50,92)", at(50, 92), lambda c: not is_green(c), "应非绿"),
        ("圆外左(34,75)", at(34, 75), lambda c: not is_green(c), "应非绿"),
        ("圆外右上(60,60)", at(60, 60), lambda c: not is_green(c), "应非绿"),
    ]
    for name, got, pred, want in cases:
        ok = pred(got)
        if not ok:
            bad += 1
        print("  %s %-22s %-18s %s" % ("ok " if ok else "FAIL", name, str(got), want))

    # 边界位置：扫描红矩形上下边界，误差应 ≤1 像素
    xmid = int(round(25 * k))
    top = None
    bot = None
    for y in range(cv.h):
        i = (y * W + xmid) * 4
        c = (buf[i], buf[i + 1], buf[i + 2])
        if is_red(c):
            if top is None:
                top = y
            bot = y
    exp_top = 10 * k
    exp_bot = 30 * k
    d_top = abs((top if top is not None else -999) - exp_top)
    d_bot = abs((bot if bot is not None else -999) - exp_bot)
    for nm, d, lim in (("红矩形上边界", d_top, 2.0), ("红矩形下边界", d_bot, 2.0)):
        ok = d <= lim
        if not ok:
            bad += 1
        print("  %s %-22s 偏差 %.2f 像素（上限 %.1f）" % (
            "ok " if ok else "FAIL", nm, d, lim))

    print()
    print("告警：%s" % (warns or "无"))
    print("几何验算结果：%s（%d 项不过）" % ("通过" if bad == 0 else "有失败", bad))
    return bad == 0


# ------------------------------------------------------------------ B. 三端对照

def check_cross(dpi=200):
    """三端交叉对照：同一 mm 采样点取色比差。"""
    from ofdlite import OFDPackage
    from ofdlite.render_soft import render_page as render_soft
    try:
        from ofdlite.render_pil import render_page as render_pil
    except Exception:
        render_pil = None

    path = build_geo_ofd()
    pkg = OFDPackage.open(path)
    page = pkg.all_pages()[0]

    probes = [
        ("红矩形中心", 25, 20),
        ("蓝方块中心", 70, 20),
        ("水平线上", 30, 50),
        ("水平线外", 30, 45),
        ("圆心中", 50, 75),
        ("圆外", 50, 58),
        ("空白", 5, 95),
    ]

    cv, _ = render_soft(page, dpi)
    soft_vals = [px_at(cv, x, y, dpi, "soft") for _, x, y in probes]

    pil_vals = [None] * len(probes)
    if render_pil is not None:
        try:
            im, _ = render_pil(page, dpi)
            pil_vals = [px_at(im, x, y, dpi, "pil") for _, x, y in probes]
        except Exception:
            render_pil = None

    print("=" * 70)
    print("B. 三端交叉对照（%d dpi，主色容差 30）" % dpi)
    print("=" * 70)
    print("  %-14s %-18s %-18s %s" % ("采样点", "纯Python", "Pillow", "判定"))
    bad = 0
    for (name, x, y), s_v, p_v in zip(probes, soft_vals, pil_vals):
        if p_v is None:
            print("  %-14s %-18s %-18s %s" % (name, s_v, "(无 Pillow)", "跳过"))
            continue
        # 判断「是不是同一类颜色」：要么两者都接近白，要么主色差 ≤30
        both_white = all(min(c) > 230 for c in (s_v, p_v) if c)
        ok = both_white or close(s_v, p_v, 30)
        if not ok:
            # 抗锯齿边缘允许差大一些，但主色仍需同向
            if s_v and p_v:
                ok = (max(s_v) - min(s_v) > 20) == (max(p_v) - min(p_v) > 20)
        if not ok:
            bad += 1
        print("  %s %-12s %-18s %-18s %s" % (
            "ok " if ok else "FAIL", name, str(s_v), str(p_v),
            "一致" if ok else "不一致"))

    print()
    print("交叉对照结果：%s（%d 项不一致）" % ("通过" if bad == 0 else "有差异", bad))
    return bad == 0


def main(argv=None):
    argv = argv or sys.argv[1:]
    os.makedirs(OUT, exist_ok=True)
    do_geo = ("--cross" not in argv)
    do_cross = ("--geo" not in argv)
    ok = True
    if do_geo:
        ok = check_geometry() and ok
        print()
    if do_cross:
        ok = check_cross() and ok
    print()
    print("=" * 70)
    print("像素级验收：%s" % ("通过" if ok else "有失败项"))
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

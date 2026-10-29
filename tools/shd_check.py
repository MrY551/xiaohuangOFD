# -*- coding: utf-8 -*-
"""渐变三端一致性验证。

做三件事：
  1. 造一份带三种 MapType 的渐变 OFD（轴向；径向加 --radial）
  2. Pillow 与 Qt 两端各渲一张，逐点采样比对（同一 mm 坐标，差值 ≤ 容差）
  3. 打印每端的采样值，肉眼也能核

用法：
    python tools/shd_check.py                 # 轴向 Direct/Reflect/Repeat
    python tools/shd_check.py --radial        # 径向
    python tools/shd_check.py --keep          # 保留中间文件
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NS = "http://www.ofdspec.org/2016"
DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'
OUT = os.path.join(ROOT, "out")


def _ofd_xml():
    return (DECL + '<ofd:OFD xmlns:ofd="%s" DocType="OFD" Version="1.0"><ofd:DocBody>'
            '<ofd:DocInfo><ofd:DocID>shd-check</ofd:DocID><ofd:Title>渐变三端验证</ofd:Title>'
            '</ofd:DocInfo><ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot></ofd:DocBody></ofd:OFD>\n') % NS


def _doc_xml():
    return (DECL + '<ofd:Document xmlns:ofd="%s"><ofd:CommonData><ofd:MaxUnitID>60</ofd:MaxUnitID>'
            '<ofd:PageArea><ofd:PhysicalBox>0 0 140 90</ofd:PhysicalBox></ofd:PageArea>'
            '<ofd:PublicRes>PublicRes.xml</ofd:PublicRes></ofd:CommonData>'
            '<ofd:Pages><ofd:Page ID="10" BaseLoc="Pages/Page_0/Content.xml"/></ofd:Pages>'
            '</ofd:Document>\n') % NS


def _res_xml():
    return (DECL + '<ofd:Res xmlns:ofd="%s" BaseLoc="Res"><ofd:ColorSpaces>'
            '<ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/>'
            '</ofd:ColorSpaces></ofd:Res>\n') % NS


def _axial(oid, x, y, w, h, extra=""):
    tpl = ('<ofd:PathObject ID="{oid}" Boundary="{x} {y} {w} {h}" Fill="true" Stroke="false">'
           '<ofd:FillColor><ofd:AxialShd StartPoint="0 0" EndPoint="{w} 0" {extra}>'
           '<ofd:Segment Position="0"><ofd:Color Value="255 0 0" ColorSpace="1"/></ofd:Segment>'
           '<ofd:Segment Position="1"><ofd:Color Value="0 0 255" ColorSpace="1"/></ofd:Segment>'
           '</ofd:AxialShd></ofd:FillColor>'
           '<ofd:AbbreviatedData>M 0 0 L {w} 0 L {w} {h} L 0 {h} C</ofd:AbbreviatedData>'
           '</ofd:PathObject>')
    return tpl.format(oid=oid, x=x, y=y, w=w, h=h, extra=extra)


def _radial(oid, x, y, w, h, extra=""):
    tpl = ('<ofd:PathObject ID="{oid}" Boundary="{x} {y} {w} {h}" Fill="true" Stroke="false">'
           '<ofd:FillColor><ofd:RadialShd StartPoint="{cx} {cy}" EndPoint="{cx} {cy}" '
           'StartRadius="0" EndRadius="{r}" {extra}>'
           '<ofd:Segment Position="0"><ofd:Color Value="255 255 0" ColorSpace="1"/></ofd:Segment>'
           '<ofd:Segment Position="1"><ofd:Color Value="0 0 255" ColorSpace="1"/></ofd:Segment>'
           '</ofd:RadialShd></ofd:FillColor>'
           '<ofd:AbbreviatedData>M 0 0 L {w} 0 L {w} {h} L 0 {h} C</ofd:AbbreviatedData>'
           '</ofd:PathObject>')
    return tpl.format(oid=oid, x=x, y=y, w=w, h=h,
                      cx=w / 2.0, cy=h / 2.0, r=min(w, h) / 2.0, extra=extra)


def build(radial: bool) -> str:
    os.makedirs(OUT, exist_ok=True)
    if radial:
        body = (_radial(21, 10, 5, 120, 38)
                + _radial(22, 10, 47, 120, 38, 'MapType="Repeat" MapUnit="15"'))
    else:
        body = (_axial(21, 10, 10, 120, 20)
                + _axial(22, 10, 35, 120, 20, 'MapType="Reflect" MapUnit="60"')
                + _axial(23, 10, 60, 120, 20, 'MapType="Repeat" MapUnit="40"'))
    content = (DECL + '<ofd:Page xmlns:ofd="%s"><ofd:Area>'
               '<ofd:PhysicalBox>0 0 140 90</ofd:PhysicalBox></ofd:Area>'
               '<ofd:Content><ofd:Layer ID="20">' + body +
               '</ofd:Layer></ofd:Content></ofd:Page>\n') % NS
    path = os.path.join(OUT, "shd_radial.ofd" if radial else "shd_test.ofd")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("OFD.xml", _ofd_xml())
        z.writestr("Doc_0/Document.xml", _doc_xml())
        z.writestr("Doc_0/PublicRes.xml", _res_xml())
        z.writestr("Doc_0/Pages/Page_0/Content.xml", content)
    return path


# 采样点（mm）：每行取左中右三点
PROBES = {
    "axial": [(15, 20), (60, 20), (125, 20),
              (15, 45), (60, 45), (125, 45),
              (15, 70), (60, 70), (125, 70)],
    "radial": [(70, 24), (30, 24), (110, 24), (70, 8),
               (70, 66), (30, 66), (110, 66)],
}


def sample_pil(path, dpi, probes):
    from ofdlite import OFDPackage
    from ofdlite.render_pil import render_page
    img, warn = render_page(OFDPackage.open(path).all_pages()[0], dpi=dpi)
    px = img.load()
    out = []
    for xmm, ymm in probes:
        out.append(px[int(xmm * dpi / 25.4), int(ymm * dpi / 25.4)])
    return out, warn, img


def sample_qt(path, dpi, probes):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage, QPainter
    from ofdlite import OFDPackage
    from ofdlite.scene import build_page_scene
    from app.qt_renderer import QtSceneRenderer

    app = QApplication.instance() or QApplication([])
    sc = build_page_scene(OFDPackage.open(path).all_pages()[0])
    zoom = dpi / 25.4
    img = QImage(int(round(sc.size.w * zoom)), int(round(sc.size.h * zoom)),
                 QImage.Format_RGB32)
    p = QPainter(img)
    r = QtSceneRenderer()
    r.render(p, sc.to_dict(), zoom)
    p.end()
    out = []
    for xmm, ymm in probes:
        c = img.pixelColor(int(xmm * zoom), int(ymm * zoom))
        out.append((c.red(), c.green(), c.blue()))
    return out, r.warnings


def build_pattern() -> str:
    """底纹测试：8x8mm 单元（上蓝线 + 下红块），按 10x10 平铺、行与列都镜像。"""
    NS = "http://www.ofdspec.org/2016"
    D = '<?xml version="1.0" encoding="UTF-8"?>\n'
    ofd = D + ('<ofd:OFD xmlns:ofd="%s" DocType="OFD" Version="1.0"><ofd:DocBody>'
               '<ofd:DocInfo><ofd:DocID>pt</ofd:DocID><ofd:Title>底纹</ofd:Title></ofd:DocInfo>'
               '<ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot></ofd:DocBody></ofd:OFD>\n') % NS
    doc = D + ('<ofd:Document xmlns:ofd="%s"><ofd:CommonData><ofd:MaxUnitID>60</ofd:MaxUnitID>'
               '<ofd:PageArea><ofd:PhysicalBox>0 0 120 80</ofd:PhysicalBox></ofd:PageArea>'
               '<ofd:PublicRes>PublicRes.xml</ofd:PublicRes></ofd:CommonData>'
               '<ofd:Pages><ofd:Page ID="10" BaseLoc="Pages/Page_0/Content.xml"/></ofd:Pages>'
               '</ofd:Document>\n') % NS
    res = D + ('<ofd:Res xmlns:ofd="%s" BaseLoc="Res"><ofd:ColorSpaces>'
               '<ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/>'
               '</ofd:ColorSpaces></ofd:Res>\n') % NS
    cell = ('<ofd:CellContent>'
            '<ofd:PathObject ID="31" Boundary="1 1 6 6" Fill="true" Stroke="false">'
            '<ofd:FillColor Value="220 60 60" ColorSpace="1"/>'
            '<ofd:AbbreviatedData>M 0 0 L 6 0 L 6 6 L 0 6 C</ofd:AbbreviatedData></ofd:PathObject>'
            '<ofd:PathObject ID="32" Boundary="0 0 8 1" Fill="true" Stroke="false">'
            '<ofd:FillColor Value="60 60 220" ColorSpace="1"/>'
            '<ofd:AbbreviatedData>M 0 0 L 8 0 L 8 1 L 0 1 C</ofd:AbbreviatedData></ofd:PathObject>'
            '</ofd:CellContent>')
    body = ('<ofd:PathObject ID="21" Boundary="10 10 100 60" Fill="true" Stroke="false">'
            '<ofd:FillColor><ofd:Pattern Width="8" Height="8" XStep="10" YStep="10" '
            'ReflectMethod="RowAndColumn">' + cell + '</ofd:Pattern></ofd:FillColor>'
            '<ofd:AbbreviatedData>M 0 0 L 100 0 L 100 60 L 0 60 C</ofd:AbbreviatedData>'
            '</ofd:PathObject>')
    content = (D + '<ofd:Page xmlns:ofd="%s"><ofd:Area>'
               '<ofd:PhysicalBox>0 0 120 80</ofd:PhysicalBox></ofd:Area>'
               '<ofd:Content><ofd:Layer ID="20">' + body +
               '</ofd:Layer></ofd:Content></ofd:Page>\n') % NS
    os.makedirs(os.path.join(OUT, "pt_test"), exist_ok=True)
    path = os.path.join(OUT, "pt_test.ofd")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("OFD.xml", ofd)
        z.writestr("Doc_0/Document.xml", doc)
        z.writestr("Doc_0/PublicRes.xml", res)
        z.writestr("Doc_0/Pages/Page_0/Content.xml", content)
    return path


def check_pattern():
    """底纹：验证相邻格交替镜像（这才是 ReflectMethod 的可判定证据）。"""
    from ofdlite import OFDPackage
    from ofdlite.scene import build_page_scene
    from ofdlite.render_pil import render_page

    path = build_pattern()
    img, warn = render_page(OFDPackage.open(path).all_pages()[0], dpi=200)
    px = img.load()
    k = 200 / 25.4

    def at(xmm, ymm):
        return px[int(xmm * k), int(ymm * k)]

    BLUE = (60, 60, 220)
    RED = (219, 60, 60)

    def close(c, t, tol=12):
        return all(abs(c[i] - t[i]) <= tol for i in range(3))

    rows = []
    # 平铺从 y=0 起算，所以 y=10.5 落在**第 2 个** tile（row=1，已上下翻转），
    # y=20.5 落在第 3 个（row=2，正常）。判断时按 row 的奇偶来定期望，
    # 不是按采样序号的奇偶——这两者差一位。
    for i, ymm in enumerate((10.5, 20.5, 30.5)):
        c = at(14, ymm)
        row = (ymm - 0.5) // 10          # tile 序号
        expect_blue = (int(row) % 2 == 0)
        ok = close(c, BLUE) if expect_blue else (not close(c, BLUE))
        rows.append((i, ymm, c, "蓝线" if expect_blue else "翻转(应无蓝线)", ok))
    # 方块中心三格都应是红
    for i, ymm in enumerate((14, 24, 34)):
        c = at(14, ymm)
        rows.append((i, ymm, c, "红块中心", close(c, RED)))

    print("底纹 %s" % os.path.relpath(path, ROOT))
    bad = 0
    for i, ymm, c, what, ok in rows:
        print("  格%d y=%-5.1f %-22s %s %s" % (i, ymm, what, str(c), "ok" if ok else "FAIL"))
        if not ok:
            bad += 1
    print("Pillow 告警: %s" % (warn or "无"))
    if bad:
        print("结果: 失败 %d 处" % bad)
        return 1
    print("结果: 底纹平铺与镜像（行方向）正确")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--radial", action="store_true")
    ap.add_argument("--pattern", action="store_true")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--tol", type=int, default=10,
                    help="每通道允许差值。两端都只做 256 级量化 + 抗锯齿，"
                         "渐变变化最快的地方会有几级差，10 足够区分真错与取整")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)

    if args.pattern:
        return check_pattern()

    path = build(args.radial)
    print("测试文件: %s" % os.path.relpath(path, ROOT))
    probes = PROBES["radial" if args.radial else "axial"]

    pil, warn_p, img = sample_pil(path, args.dpi, probes)
    qt, warn_q = sample_qt(path, args.dpi, probes)
    img.save(os.path.join(OUT, "shd_pil.png"))

    print()
    print("  %-12s %-18s %-18s %s" % ("采样点", "Pillow", "Qt", "差"))
    worst = 0
    bad = 0
    for (xmm, ymm), a, b in zip(probes, pil, qt):
        d = max(abs(a[i] - b[i]) for i in range(3))
        worst = max(worst, d)
        flag = "  <-- 超差" if d > args.tol else ""
        if d > args.tol:
            bad += 1
        print("  (%5.1f,%5.1f)  %-18s %-18s %d%s"
              % (xmm, ymm, str(a), str(b), d, flag))

    print()
    print("Pillow 告警: %s" % (warn_p or "无"))
    print("Qt     告警: %s" % (warn_q or "无"))
    print("最大通道差: %d（容差 %d）" % (worst, args.tol))
    if bad:
        print("结果: 失败 %d 处" % bad)
        return 1
    print("结果: 三端一致（此脚本比 Pillow 与 Qt；Canvas 用 cdp_web_test --shot 目视核对）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

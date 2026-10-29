# -*- coding: utf-8 -*-
"""造一个带 EdgeFlag=1 的网格高洛德渐变，验证对角线映射。

8.3.4.5 正文说网格高洛德的 EdgeFlag 固定为 1，所以这个用例就是标准要求的形态。
"""
import sys
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NS = "http://www.ofdspec.org/2016"
DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'

ofd = DECL + (
    '<ofd:OFD xmlns:ofd="%s" DocType="OFD" Version="1.0"><ofd:DocBody>'
    '<ofd:DocInfo><ofd:DocID>lg2</ofd:DocID><ofd:Title>lg2</ofd:Title></ofd:DocInfo>'
    '<ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot></ofd:DocBody></ofd:OFD>\n') % NS

doc = DECL + (
    '<ofd:Document xmlns:ofd="%s"><ofd:CommonData><ofd:MaxUnitID>60</ofd:MaxUnitID>'
    '<ofd:PageArea><ofd:PhysicalBox>0 0 120 90</ofd:PhysicalBox></ofd:PageArea>'
    '<ofd:PublicRes>PublicRes.xml</ofd:PublicRes></ofd:CommonData>'
    '<ofd:Pages><ofd:Page ID="10" BaseLoc="Pages/Page_0/Content.xml"/></ofd:Pages>'
    '</ofd:Document>\n') % NS

res = DECL + (
    '<ofd:Res xmlns:ofd="%s" BaseLoc="Res"><ofd:ColorSpaces>'
    '<ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/>'
    '</ofd:ColorSpaces></ofd:Res>\n') % NS

rows = [
    ((10, 55, 100), ("255 0 0", "255 165 0", "0 255 0")),
    ((10, 55, 100), ("255 0 255", "255 255 255", "0 255 255")),
]
pts = ""
for ys, (xs, colors) in zip((10, 45), rows):
    for x, c in zip(xs, colors):
        pts += ('<ofd:Point X="%d" Y="%d">'
                '<ofd:Color Value="%s" ColorSpace="1"/></ofd:Point>') % (x, ys, c)

body = (
    '<ofd:PathObject ID="21" Boundary="10 10 110 40" Fill="true" Stroke="false">'
    '<ofd:FillColor><ofd:LaGourandShd VerticesPerRow="3" EdgeFlag="1">'
    '%s'
    '</ofd:LaGourandShd></ofd:FillColor>'
    '<ofd:AbbreviatedData>M 0 0 L 110 0 L 110 40 L 0 40 C</ofd:AbbreviatedData>'
    '</ofd:PathObject>') % pts

content = DECL + (
    '<ofd:Page xmlns:ofd="%s"><ofd:Area>'
    '<ofd:PhysicalBox>0 0 120 90</ofd:PhysicalBox></ofd:Area>'
    '<ofd:Content><ofd:Layer ID="20">%s</ofd:Layer></ofd:Content></ofd:Page>\n') % (NS, body)

path = os.path.join(ROOT, "out", "lg2_test.ofd")
with zipfile.ZipFile(path, "w") as z:
    z.writestr("OFD.xml", ofd)
    z.writestr("Doc_0/Document.xml", doc)
    z.writestr("Doc_0/PublicRes.xml", res)
    z.writestr("Doc_0/Pages/Page_0/Content.xml", content)

from ofdlite import OFDPackage
from ofdlite.scene import build_page_scene
from ofdlite.render_soft import render_page

pkg = OFDPackage.open(path)
sc = build_page_scene(pkg.all_pages()[0])
for o in sc.ops:
    f = o.get("fill")
    if isinstance(f, dict) and f.get("kind") in ("gouraud", "lattice"):
        print("kind=%s vpr=%s edgeflag=%s 点数=%d" % (
            f.get("kind"), f.get("vpr"), f.get("edgeflag"),
            len(f.get("points") or [])))

cv, w = render_page(pkg.all_pages()[0], 150)
b = cv.buf
nz = sum(1 for k in range(0, len(b), 4)
         if not (b[k] > 250 and b[k + 1] > 250 and b[k + 2] > 250))
print("EdgeFlag=1 网格: 画布 %dx%d 非白 %d 告警 %s" % (cv.w, cv.h, nz, w or "无"))
cv.to_png(os.path.join(ROOT, "out", "lg2_test.png"))


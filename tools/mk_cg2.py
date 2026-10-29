# -*- coding: utf-8 -*-
"""造一个复合对象（第 13 章），验证 ResourceID -> CT_VectorG -> Content 的展开。

表 49：复合对象的内容在 ResourceID 指向的矢量图像资源里。
表 50：矢量图像的 Width/Height 是裁剪边界，Content 是页面块。
"""
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NS = "http://www.ofdspec.org/2016"
DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'

ofd = DECL + (
    '<ofd:OFD xmlns:ofd="%s" DocType="OFD" Version="1.0"><ofd:DocBody>'
    '<ofd:DocInfo><ofd:DocID>cg</ofd:DocID><ofd:Title>cg</ofd:Title></ofd:DocInfo>'
    '<ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot></ofd:DocBody></ofd:OFD>\n') % NS

doc = DECL + (
    '<ofd:Document xmlns:ofd="%s"><ofd:CommonData><ofd:MaxUnitID>80</ofd:MaxUnitID>'
    '<ofd:PageArea><ofd:PhysicalBox>0 0 100 80</ofd:PhysicalBox></ofd:PageArea>'
    '<ofd:PublicRes>PublicRes.xml</ofd:PublicRes></ofd:CommonData>'
    '<ofd:Pages><ofd:Page ID="10" BaseLoc="Pages/Page_0/Content.xml"/></ofd:Pages>'
    '</ofd:Document>\n') % NS

# 矢量图像资源：20x20 的绿方块 + 一个溢出到 40 的红条（用来验裁剪）
# 元素名照抄表18 / 附录 A 的 CompositeGraphicUnit——写成 CompositeGraphUnit
# 会让这份样本"通过"而标准件全空（2026-10-30 踩过，见 docs 核对记录 附三十七）
vg_content = (
    '<ofd:PathObject ID="60" Boundary="0 0 20 20" Fill="true" Stroke="false">'
    '<ofd:FillColor Value="0 200 0" ColorSpace="1"/>'
    '<ofd:AbbreviatedData>M 0 0 L 20 0 L 20 20 L 0 20 C</ofd:AbbreviatedData>'
    '</ofd:PathObject>'
    '<ofd:PathObject ID="61" Boundary="0 0 40 4" Fill="true" Stroke="false">'
    '<ofd:FillColor Value="255 0 0" ColorSpace="1"/>'
    '<ofd:AbbreviatedData>M 0 0 L 40 0 L 40 4 L 0 4 C</ofd:AbbreviatedData>'
    '</ofd:PathObject>')

res = DECL + (
    '<ofd:Res xmlns:ofd="%s" BaseLoc="Res">'
    '<ofd:ColorSpaces><ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/>'
    '</ofd:ColorSpaces>'
    '<ofd:CompositeGraphicUnits>'
    '<ofd:CompositeGraphicUnit ID="70" Width="20" Height="20">'
    '<ofd:Content>%s</ofd:Content>'
    '</ofd:CompositeGraphicUnit>'
    '</ofd:CompositeGraphicUnits>'
    '</ofd:Res>\n') % (NS, vg_content)

# 页面：一个 CompositeObject 引用 70，落在 (30,30)
body = (
    '<ofd:CompositeObject ID="21" ResourceID="70" Boundary="30 30 20 20"/>')

content = DECL + (
    '<ofd:Page xmlns:ofd="%s"><ofd:Area>'
    '<ofd:PhysicalBox>0 0 100 80</ofd:PhysicalBox></ofd:Area>'
    '<ofd:Content><ofd:Layer ID="20">%s</ofd:Layer></ofd:Content>'
    '</ofd:Page>\n') % (NS, body)

path = os.path.join(ROOT, "out", "cg2_test.ofd")
with zipfile.ZipFile(path, "w") as z:
    z.writestr("OFD.xml", ofd)
    z.writestr("Doc_0/Document.xml", doc)
    z.writestr("Doc_0/PublicRes.xml", res)
    z.writestr("Doc_0/Pages/Page_0/Content.xml", content)

from ofdlite import OFDPackage
from ofdlite.scene import build_page_scene
from ofdlite.render_soft import render_page

pkg = OFDPackage.open(path)
pg = pkg.all_pages()[0]
for obj in pg.layers[0].flat_objects():
    if type(obj).__name__ == "CompositeObject":
        print("复合对象 #%s ResourceID=%s" % (obj.id, obj.resource_id()))
        vg = obj.vector_g()
        print("  vector_g:", None if vg is None else
              {"w": vg["w"], "h": vg["h"], "has_content": vg["content"] is not None})
        print("  展开出的对象数:", len(obj.content_objects()))

sc = build_page_scene(pg)
paths = [o for o in sc.ops if o.get("t") == "path"]
print("scene 里 path op 数:", len(paths))
for o in paths:
    print("   subs[0][:4]:", (o.get("subs") or [[]])[0][:4])

cv, w = render_page(pg, 150)
b = cv.buf
nz = sum(1 for k in range(0, len(b), 4)
         if not (b[k] > 250 and b[k + 1] > 250 and b[k + 2] > 250))
print("非白 %d 告警 %s" % (nz, w or "无"))
cv.to_png(os.path.join(ROOT, "out", "cg2_test.png"))

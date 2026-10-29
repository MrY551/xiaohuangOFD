# -*- coding: utf-8 -*-
"""标准特性探针：生成带「模板页」「裁剪路径」的 OFD，看本实现的实际表现。

    python tools/std_gap_probe.py -o out/probe

用途是对照 GB/T 33190-2016 做差距取证：这些元素在真实文件里常见，
本实现不支持，症状是什么、影响多大，用图说话。
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


def ofd_xml(title):
    # 表3：Version 取值固定 "1.0"；DocType=OFD。探针也要先合规，
    # 否则验出来的问题分不清是"实现对"还是"样本本身不合规"。
    return DECL + (
        '<ofd:OFD xmlns:ofd="%s" DocType="OFD" Version="1.0">\n'
        '  <ofd:DocBody>\n'
        '    <ofd:DocInfo><ofd:DocID>PROBE-1</ofd:DocID>'
        '<ofd:Title>%s</ofd:Title></ofd:DocInfo>\n'
        '    <ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot>\n'
        '  </ofd:DocBody>\n'
        '</ofd:OFD>\n'
    ) % (NS, title)


def document_xml(with_template=False):
    tpl = ('    <ofd:TemplatePage ID="50" Name="页眉模板"'
           ' BaseLoc="Tpl_0/Content.xml"/>\n') if with_template else ""
    return DECL + (
        '<ofd:Document xmlns:ofd="%s">\n'
        '  <ofd:CommonData>\n'
        '    <ofd:MaxUnitID>90</ofd:MaxUnitID>\n'
        '    <ofd:PageArea><ofd:PhysicalBox>0 0 210 297</ofd:PhysicalBox></ofd:PageArea>\n'
        '    <ofd:PublicRes>PublicRes.xml</ofd:PublicRes>\n'
        '%s'
        '  </ofd:CommonData>\n'
        '  <ofd:Pages>\n'
        '    <ofd:Page ID="80" BaseLoc="Pages/Page_0/Content.xml"/>\n'
        '  </ofd:Pages>\n'
        '</ofd:Document>\n'
    ) % (NS, tpl)


def template_content():
    """模板页：一条页眉横线 + 页眉文字 + 水印。"""
    return DECL + (
        '<ofd:Page xmlns:ofd="%s">\n'
        '  <ofd:Area><ofd:PhysicalBox>0 0 210 297</ofd:PhysicalBox></ofd:Area>\n'
        '  <ofd:Content>\n'
        '    <ofd:Layer ID="51" Type="Body">\n'
        '      <ofd:PathObject ID="52" Boundary="20 15 170 0.8" LineWidth="0.8"'
        ' Stroke="true" StrokeColor="200 0 0" ColorSpace="1">\n'
        '        <ofd:Path Data="M 0 0 L 170 0"/>\n'
        '      </ofd:PathObject>\n'
        '      <ofd:TextObject ID="53" Boundary="20 4 120 9" Font="2" Size="5"'
        ' Fill="true" FillColor="200 0 0" ColorSpace="1">\n'
        '        <ofd:TextCode X="0" Y="7" Text="【模板页页眉 红头】"/>\n'
        '      </ofd:TextObject>\n'
        '      <ofd:TextObject ID="54" Boundary="60 140 90 20" Font="3" Size="16"'
        ' Fill="true" FillColor="230 230 230" ColorSpace="1"'
        ' CTM="0.7071 0.7071 -0.7071 0.7071 148 -66">\n'
        '        <ofd:TextCode X="0" Y="12" Text="水印 内部资料"/>\n'
        '      </ofd:TextObject>\n'
        '    </ofd:Layer>\n'
        '  </ofd:Content>\n'
        '</ofd:Page>\n'
    ) % NS


def page_pagablock_content():
    """页面：只有一个 PageBlock（引模板）+ 一行正文。"""
    return DECL + (
        '<ofd:Page xmlns:ofd="%s">\n'
        '  <ofd:Area><ofd:PhysicalBox>0 0 210 297</ofd:PhysicalBox></ofd:Area>\n'
        '  <ofd:Content>\n'
        '    <ofd:Layer ID="60" Type="Body">\n'
        '      <ofd:PageBlock ID="61" Boundary="0 0 210 297">\n'
        '        <ofd:Template TemplateID="50" ZOrder="Background"/>\n'
        '      </ofd:PageBlock>\n'
        '      <ofd:TextObject ID="62" Boundary="20 40 170 12" Font="3" Size="6"'
        ' Fill="true" FillColor="0 0 0" ColorSpace="1">\n'
        '        <ofd:TextCode X="0" Y="8" Text="正文：这一页用了模板页做页眉和水印"/>\n'
        '      </ofd:TextObject>\n'
        '    </ofd:Layer>\n'
        '  </ofd:Content>\n'
        '</ofd:Page>\n'
    ) % NS


def page_clip_content():
    """页面：一个带 ClipPath 的红色大方块 + 一个不裁剪的对照组。"""
    return DECL + (
        '<ofd:Page xmlns:ofd="%s">\n'
        '  <ofd:Area><ofd:PhysicalBox>0 0 210 297</ofd:PhysicalBox></ofd:Area>\n'
        '  <ofd:Content>\n'
        '    <ofd:Layer ID="70" Type="Body">\n'
        '      <ofd:TextObject ID="71" Boundary="20 20 170 10" Font="3" Size="5"'
        ' Fill="true" FillColor="0 0 0" ColorSpace="1">\n'
        '        <ofd:TextCode X="0" Y="7" Text="左：带 ClipPath（应只露出中间小方块）"/>\n'
        '      </ofd:TextObject>\n'
        '      <ofd:PathObject ID="72" Boundary="20 40 80 60" Fill="true"'
        ' FillColor="220 40 40" ColorSpace="1" Stroke="true"'
        ' StrokeColor="0 0 0" LineWidth="0.4" ClipPath="73">\n'
        '        <ofd:Path Data="M 0 0 L 80 0 L 80 60 L 0 60 Z"/>\n'
        '      </ofd:PathObject>\n'
        '      <ofd:PathObject ID="73" Boundary="40 55 30 25" LineWidth="0.3"'
        ' Stroke="true" StrokeColor="0 120 200" ColorSpace="1">\n'
        '        <ofd:Path Data="M 0 0 L 30 0 L 30 25 L 0 25 Z"/>\n'
        '      </ofd:PathObject>\n'
        '      <ofd:TextObject ID="74" Boundary="120 20 80 10" Font="3" Size="5"'
        ' Fill="true" FillColor="0 0 0" ColorSpace="1">\n'
        '        <ofd:TextCode X="0" Y="7" Text="右：无裁剪对照组"/>\n'
        '      </ofd:TextObject>\n'
        '      <ofd:PathObject ID="75" Boundary="120 40 80 60" Fill="true"'
        ' FillColor="220 40 40" ColorSpace="1" Stroke="true"'
        ' StrokeColor="0 0 0" LineWidth="0.4">\n'
        '        <ofd:Path Data="M 0 0 L 80 0 L 80 60 L 0 60 Z"/>\n'
        '      </ofd:PathObject>\n'
        '    </ofd:Layer>\n'
        '  </ofd:Content>\n'
        '</ofd:Page>\n'
    ) % NS


def public_res():
    return DECL + (
        '<ofd:Res xmlns:ofd="%s" BaseLoc="Res">\n'
        '  <ofd:ColorSpaces>\n'
        '    <ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/>\n'
        '  </ofd:ColorSpaces>\n'
        '  <ofd:Fonts>\n'
        '    <ofd:Font ID="2" FontName="SimHei" FamilyName="黑体" Charset="unicode"/>\n'
        '    <ofd:Font ID="3" FontName="SimSun" FamilyName="宋体" Charset="unicode"/>\n'
        '  </ofd:Fonts>\n'
        '</ofd:Res>\n'
    ) % NS


def build_color_sample(out_dir):
    """8.3.1 / 8.3.2 / 表25 / 表26 / 表27：颜色的四种写法 + 调色板 + BPC。

    五个色块从左到右，标准期望值写进 print，肉眼看渲染图即可核对：
      1  "#11 #22 #33"           表27 的逐通道十六进制 RGB -> (17,34,51) 深蓝黑
      2  "#FF"  + ColorSpace=3   表27 的 GRAY 写法 -> 白
      3  "15 0 0" + BPC=4        表27 的通道上限 2^4-1 -> 纯红
      4  Index=1 调色板           表26 的 Index 取色 -> 纯蓝
      5  "0 255 255 0" + CS=5    资源里 CS5 是 CMYK -> 纯红（BPC=8 时 255 才是满墨）
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "probe_color.ofd")
    boxes = [
        (10, 20, 30, 30, '<ofd:FillColor Value="#11 #22 #33" ColorSpace="1"/>'),
        (50, 20, 30, 30, '<ofd:FillColor Value="#FF" ColorSpace="3"/>'),
        (90, 20, 30, 30, '<ofd:FillColor Value="15 0 0" ColorSpace="4"/>'),
        (130, 20, 30, 30, '<ofd:FillColor Index="1" ColorSpace="2"/>'),
        (10, 60, 30, 30, '<ofd:FillColor Value="0 255 255 0" ColorSpace="5"/>'),
    ]
    objs = []
    for i, (x, y, w, h, fill) in enumerate(boxes):
        objs.append(
            '<ofd:PathObject ID="%d" Boundary="%d %d %d %d" Fill="true" Stroke="false">'
            '%s<ofd:AbbreviatedData>M 0 0 L %d 0 L %d %d L 0 %d C</ofd:AbbreviatedData>'
            '</ofd:PathObject>' % (200 + i, x, y, w, h, fill, w, w, h, h))
    page = DECL + (
        '<ofd:Page xmlns:ofd="%s">'
        '<ofd:Area><ofd:PhysicalBox>0 0 180 110</ofd:PhysicalBox></ofd:Area>'
        '<ofd:Content><ofd:Layer ID="210">%s</ofd:Layer></ofd:Content></ofd:Page>'
        % (NS, "".join(objs)))
    res = DECL + (
        '<ofd:Res xmlns:ofd="%s" BaseLoc="Res">'
        '<ofd:ColorSpaces>'
        '<ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/>'
        '<ofd:ColorSpace ID="2" Type="RGB" BitsPerComponent="8">'
        '<ofd:Palette>'
        '<ofd:CV>255 0 0</ofd:CV>'
        '<ofd:CV>0 0 255</ofd:CV>'
        '</ofd:Palette></ofd:ColorSpace>'
        '<ofd:ColorSpace ID="3" Type="GRAY" BitsPerComponent="8"/>'
        '<ofd:ColorSpace ID="4" Type="RGB" BitsPerComponent="4"/>'
        '<ofd:ColorSpace ID="5" Type="CMYK" BitsPerComponent="8"/>'
        '</ofd:ColorSpaces></ofd:Res>' % NS)
    z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    z.writestr("OFD.xml", ofd_xml("颜色写法探针"))
    z.writestr("Doc_0/Document.xml", document_xml(False))
    z.writestr("Doc_0/PublicRes.xml", res)
    z.writestr("Doc_0/Pages/Page_0/Content.xml", page)
    z.close()
    return path


def build_drawparam_sample(out_dir):
    """表21：DrawParam 可以用 Relative 继承基础绘制参数。

    DP10 = 红笔 + 线宽 1.0；DP20 Relative="10"，只改 Join。
    引用 DP20 的对象应当拿到「红色 + 1.0mm + 圆角」三样。
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "probe_drawparam.ofd")
    page = DECL + (
        '<ofd:Page xmlns:ofd="%s">'
        '<ofd:Area><ofd:PhysicalBox>0 0 120 60</ofd:PhysicalBox></ofd:Area>'
        '<ofd:Content><ofd:Layer ID="310">'
        '<ofd:PathObject ID="311" Boundary="10 10 100 40" DrawParam="20" Fill="false">'
        '<ofd:AbbreviatedData>M 0 0 L 100 0 L 100 40 C</ofd:AbbreviatedData>'
        '</ofd:PathObject>'
        '</ofd:Layer></ofd:Content></ofd:Page>' % NS)
    res = DECL + (
        '<ofd:Res xmlns:ofd="%s" BaseLoc="Res">'
        '<ofd:ColorSpaces><ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/>'
        '</ofd:ColorSpaces>'
        '<ofd:DrawParams>'
        '<ofd:DrawParam ID="10" LineWidth="1.0" Join="Miter" Cap="Butt">'
        '<ofd:StrokeColor Value="255 0 0" ColorSpace="1"/></ofd:DrawParam>'
        '<ofd:DrawParam ID="20" Relative="10" Join="Round"/>'
        '</ofd:DrawParams></ofd:Res>' % NS)
    z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    z.writestr("OFD.xml", ofd_xml("绘制参数继承探针"))
    z.writestr("Doc_0/Document.xml", document_xml(False))
    z.writestr("Doc_0/PublicRes.xml", res)
    z.writestr("Doc_0/Pages/Page_0/Content.xml", page)
    z.close()
    return path


def build_composite_sample(out_dir):
    """第 13 章：矢量图像资源的元素名按标准写 CompositeGraphicUnit。

    早期实现查的是 CompositeGraphUnit（少个 ic），标准件一个都读不到，
    复合对象渲染成空白；这个样本就是那条的回归用例。
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "probe_composite.ofd")
    vg = ('<ofd:PathObject ID="60" Boundary="0 0 20 20" Fill="true" Stroke="false">'
          '<ofd:FillColor Value="0 200 0" ColorSpace="1"/>'
          '<ofd:AbbreviatedData>M 0 0 L 20 0 L 20 20 L 0 20 C</ofd:AbbreviatedData>'
          '</ofd:PathObject>')
    res = DECL + (
        '<ofd:Res xmlns:ofd="%s" BaseLoc="Res">'
        '<ofd:ColorSpaces><ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/>'
        '</ofd:ColorSpaces>'
        '<ofd:CompositeGraphicUnits>'
        '<ofd:CompositeGraphicUnit ID="70" Width="20" Height="20">'
        '<ofd:Content>%s</ofd:Content>'
        '</ofd:CompositeGraphicUnit>'
        '</ofd:CompositeGraphicUnits></ofd:Res>' % (NS, vg))
    page = DECL + (
        '<ofd:Page xmlns:ofd="%s">'
        '<ofd:Area><ofd:PhysicalBox>0 0 100 80</ofd:PhysicalBox></ofd:Area>'
        '<ofd:Content><ofd:Layer ID="20">'
        '<ofd:CompositeObject ID="21" ResourceID="70" Boundary="30 30 20 20"/>'
        '</ofd:Layer></ofd:Content></ofd:Page>' % NS)
    z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    z.writestr("OFD.xml", ofd_xml("复合对象探针"))
    z.writestr("Doc_0/Document.xml", document_xml(False))
    z.writestr("Doc_0/PublicRes.xml", res)
    z.writestr("Doc_0/Pages/Page_0/Content.xml", page)
    z.close()
    return path


def build(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    made = []

    # --- 样本 1：模板页 + PageBlock
    p = os.path.join(out_dir, "probe_template.ofd")
    z = zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED)
    z.writestr("OFD.xml", ofd_xml("模板页探针").encode("utf-8"))
    z.writestr("Doc_0/Document.xml", document_xml(True).encode("utf-8"))
    z.writestr("Doc_0/PublicRes.xml", public_res().encode("utf-8"))
    z.writestr("Doc_0/Pages/Page_0/Content.xml",
               page_pagablock_content().encode("utf-8"))
    z.writestr("Doc_0/Tpl_0/Content.xml", template_content().encode("utf-8"))
    z.close()
    made.append(p)

    # --- 样本 2：ClipPath
    p = os.path.join(out_dir, "probe_clip.ofd")
    z = zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED)
    z.writestr("OFD.xml", ofd_xml("裁剪路径探针").encode("utf-8"))
    z.writestr("Doc_0/Document.xml", document_xml(False).encode("utf-8"))
    z.writestr("Doc_0/PublicRes.xml", public_res().encode("utf-8"))
    z.writestr("Doc_0/Pages/Page_0/Content.xml", page_clip_content().encode("utf-8"))
    z.close()
    made.append(p)
    return made



NS_ = "http://www.ofdspec.org/2016"
DECL_ = '<?xml version="1.0" encoding="UTF-8"?>\n'


def build_annot_sample(out_dir):
    """第15章注释：Document 引用注释列表 -> 列表按页索引 -> 分页注释文件。

    重点是 Stamp（签章）的 Appearance：签章图片就躺在 CT_PageBlock 里，
    不解析注释这条路径，页面上就少一个章。
    """
    import zipfile
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "probe_annot.ofd")
    ofd = DECL_ + (
        '<ofd:OFD xmlns:ofd="%s" DocType="OFD" Version="1.1"><ofd:DocBody>'
        '<ofd:DocInfo><ofd:DocID>PROBE-ANNOT</ofd:DocID><ofd:Title>注释探针</ofd:Title>'
        '</ofd:DocInfo><ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot>'
        '</ofd:DocBody></ofd:OFD>') % NS_
    doc = DECL_ + (
        '<ofd:Document xmlns:ofd="%s"><ofd:CommonData>'
        '<ofd:MaxUnitID>900</ofd:MaxUnitID>'
        '<ofd:PageArea><ofd:PhysicalBox>0 0 210 140</ofd:PhysicalBox></ofd:PageArea>'
        '<ofd:PublicRes>PublicRes.xml</ofd:PublicRes>'
        '<ofd:Annotations>Annots/Annotations.xml</ofd:Annotations>'
        '</ofd:CommonData><ofd:Pages>'
        '<ofd:Page ID="2" BaseLoc="Pages/Page_0/Content.xml"/>'
        '</ofd:Pages></ofd:Document>') % NS_
    page = DECL_ + (
        '<ofd:Page xmlns:ofd="%s">'
        '<ofd:Area><ofd:PhysicalBox>0 0 210 140</ofd:PhysicalBox></ofd:Area>'
        '<ofd:Content><ofd:Layer ID="3">'
        '<ofd:TextObject ID="4" Boundary="20 20 120 10" Font="59" Size="5">'
        '<ofd:TextCode X="0" Y="6">页面正文（这一页没有内容层里的章）</ofd:TextCode>'
        '</ofd:TextObject>'
        '</ofd:Layer></ofd:Content></ofd:Page>') % NS_
    idx = DECL_ + (
        '<ofd:Annotations xmlns:ofd="%s">'
        '<ofd:Page PageID="2" FileLoc="Page_0/Annot.xml"/>'
        '</ofd:Annotations>') % NS_
    annot = DECL_ + (
        '<ofd:PageAnnot xmlns:ofd="%s">'
        '<ofd:Annot ID="900" Type="Stamp" Creator="probe" LastModDate="2026-01-01">'
        '<ofd:Parameters><ofd:Parameter Name="SealName">测试印章</ofd:Parameter>'
        '</ofd:Parameters>'
        '<ofd:Appearance>'
        '<ofd:PathObject ID="901" Boundary="120 90 60 30" LineWidth="0.8"'
        ' Stroke="true" Fill="false" StrokeColor="200 30 30" ColorSpace="1">'
        '<ofd:AbbreviatedData>M 0 15 L 60 15</ofd:AbbreviatedData>'
        '</ofd:PathObject>'
        '<ofd:TextObject ID="902" Boundary="124 98 52 12" Font="59" Size="6"'
        ' Fill="true" FillColor="200 30 30" ColorSpace="1">'
        '<ofd:TextCode X="0" Y="9">签章 NO.001</ofd:TextCode>'
        '</ofd:TextObject>'
        '</ofd:Appearance></ofd:Annot>'
        '</ofd:PageAnnot>') % NS_
    res = DECL_ + (
        '<ofd:Res xmlns:ofd="%s" BaseLoc="Res">'
        '<ofd:ColorSpaces><ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/>'
        '</ofd:ColorSpaces>'
        '<ofd:Fonts><ofd:Font ID="59" FontName="SimSun" FamilyName="宋体"/>'
        '</ofd:Fonts></ofd:Res>') % NS_
    z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    z.writestr("OFD.xml", ofd.encode("utf-8"))
    z.writestr("Doc_0/Document.xml", doc.encode("utf-8"))
    z.writestr("Doc_0/PublicRes.xml", res.encode("utf-8"))
    z.writestr("Doc_0/Pages/Page_0/Content.xml", page.encode("utf-8"))
    z.writestr("Doc_0/Annots/Annotations.xml", idx.encode("utf-8"))
    z.writestr("Doc_0/Annots/Page_0/Annot.xml", annot.encode("utf-8"))
    z.close()
    return path

def _probe_object_detail(o):
    """把对象解析出来的颜色/描边/子对象打出来。

    只看"渲染了没有"抓不到"颜色算错了"——颜色空间、BPC、调色板、
    绘制参数继承这几类错误，画面都是"有东西"，只是颜色/线宽不对。
    """
    out = []
    try:
        if o.has_stroke():
            out.append("stroke: color=%s width=%.3f join=%s cap=%s"
                       % (o.stroke_color(), o.line_width(),
                          o.line_join(), o.line_cap()))
        if o.has_fill():
            out.append("fill:   color=%s" % (o.fill_color(),))
        kids = o.children()
    except Exception:
        kids = []
    if kids:
        out.append("子对象 %d：%s" % (len(kids),
                                     [(k.id, k.fill_color()) for k in kids]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=os.path.join(ROOT, "out", "probe"))
    args = ap.parse_args(argv)
    paths = build(args.out)
    paths.append(build_annot_sample(args.out))
    for maker in (build_color_sample, build_drawparam_sample,
                  build_composite_sample):
        paths.append(maker(args.out))
    from ofdlite import OFDPackage
    from ofdlite.render_pil import render_page
    from ofdlite.scene import build_page_scene

    for path in paths:
        print("=" * 70)
        print("样本:", os.path.basename(path))
        pkg = OFDPackage.open(path)
        page = pkg.all_pages()[0]
        print("  页尺寸:", page.size(), " 对象数:", len(page.objects()))
        for anno in getattr(page, "annotations", []):
            print("    注释:", anno.describe(), "参数:", anno.parameters)
        for o in page.objects():
            print("    -", o.describe())
            for line in _probe_object_detail(o):
                print("       ", line)
        sc = build_page_scene(page)
        print("  指令数:", len(sc.ops), " 告警:", sc.warnings or "无")
        img, warn = render_page(page, dpi=100)
        out_png = os.path.splitext(path)[0] + ".png"
        img.save(out_png)
        print("  渲染:", out_png, img.size)
        if warn:
            print("  渲染告警:", warn)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    return DECL + (
        '<ofd:OFD xmlns:ofd="%s" DocType="OFD" Version="1.1">\n'
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

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=os.path.join(ROOT, "out", "probe"))
    args = ap.parse_args(argv)
    paths = build(args.out)
    paths.append(build_annot_sample(args.out))
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

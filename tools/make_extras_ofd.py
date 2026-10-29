# -*- coding: utf-8 -*-
"""生成"附属结构齐全"的测试 OFD：权限 / 视图 / 书签 / 附件 / 标引 / 扩展 / 签名 / 版本，
外加第 14 章的全部五类动作与表 44 的等宽字型。

    python tools/make_extras_ofd.py out/extras_test.ofd

用途：这几章以前只"保留字节不解析"，改完之后要有东西可验。
结构按表8/9/10、表51-59、表63-73 逐条摆，元素名照抄附录 A。
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NS = "http://www.ofdspec.org/2016"
D = '<?xml version="1.0" encoding="UTF-8"?>\n'


def build(path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    ofd = D + (
        '<ofd:OFD xmlns:ofd="%s" DocType="OFD" Version="1.0">'
        '<ofd:DocBody><ofd:DocInfo>'
        '<ofd:DocID>EXTRAS-0001</ofd:DocID><ofd:Title>附属结构样本</ofd:Title>'
        '<ofd:Author>小黄</ofd:Author>'
        '</ofd:DocInfo><ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot>'
        # 附录 A：Versions 内嵌在 DocBody（在 Signatures 之前），Signatures 是 ST_Loc
        '<ofd:Versions>'
        '<ofd:Version ID="v1" Index="1" Current="true" BaseLoc="Doc_0/V1/DocVersion.xml"/>'
        '<ofd:Version ID="v2" Index="2" Current="false" BaseLoc="Doc_0/V2/DocVersion.xml"/>'
        '</ofd:Versions>'
        '<ofd:Signatures>Signs/Signatures.xml</ofd:Signatures>'
        '</ofd:DocBody></ofd:OFD>') % NS

    doc = D + (
        '<ofd:Document xmlns:ofd="%s">'
        '<ofd:CommonData>'
        '<ofd:MaxUnitID>920</ofd:MaxUnitID>'
        '<ofd:PageArea><ofd:PhysicalBox>0 0 210 297</ofd:PhysicalBox></ofd:PageArea>'
        '<ofd:PublicRes>PublicRes.xml</ofd:PublicRes>'
        '</ofd:CommonData>'
        '<ofd:Pages>'
        '<ofd:Page ID="10" BaseLoc="Pages/Page_0/Content.xml"/>'
        '<ofd:Page ID="20" BaseLoc="Pages/Page_1/Content.xml"/>'
        '</ofd:Pages>'
        '<ofd:Outlines><ofd:OutlineElem Title="第一章">'
        '<ofd:Actions><ofd:Action Event="CLICK"><ofd:Goto>'
        '<ofd:Dest Type="XYZ" PageID="10" Left="0" Top="0" Zoom="150"/>'
        '</ofd:Goto></ofd:Action></ofd:Actions>'
        '</ofd:OutlineElem></ofd:Outlines>'
        # 表8：导出与打印都禁止，份数限 2。
        # 附录 A 的 CT_Permission 把开关写成子元素（正文表8 写属性），这里按 schema 写，
        # 解析器两种都认。
        '<ofd:Permissions>'
        '<ofd:Edit>true</ofd:Edit>'
        '<ofd:Export>false</ofd:Export>'
        '<ofd:Print Printable="false" Copies="2"/>'
        '<ofd:ValidPeriod StartDate="2026-01-01T00:00:00" EndDate="2030-01-01T00:00:00"/>'
        '</ofd:Permissions>'
        # 表9：初始 120%（附录 A 里 ZoomMode 与 Zoom 是二选一，这里用 Zoom）
        '<ofd:VPreferences>'
        '<ofd:PageMode>UseOutlines</ofd:PageMode>'
        '<ofd:PageLayout>OnePage</ofd:PageLayout>'
        '<ofd:Zoom>120</ofd:Zoom>'
        '</ofd:VPreferences>'
        # 表10：两个书签
        '<ofd:Bookmarks>'
        '<ofd:Bookmark Name="封面"><ofd:Dest Type="XYZ" PageID="10" Left="10" Top="20" Zoom="100"/></ofd:Bookmark>'
        '<ofd:Bookmark Name="正文"><ofd:Dest Type="FitH" PageID="20" Top="0"/></ofd:Bookmark>'
        '</ofd:Bookmarks>'
        # 附录 A：Document 子元素顺序 = Annotations → CustomTags → Attachments → Extensions
        '<ofd:CustomTags>CustomTags.xml</ofd:CustomTags>'
        '<ofd:Attachments>Attachments.xml</ofd:Attachments>'
        '<ofd:Extensions>Extensions.xml</ofd:Extensions>'
        '</ofd:Document>') % NS

    # 第 14 章：五类动作各来一个（Goto / URI / GotoA / Sound / Movie）
    # 附录 A：CT_GraphicUnit 的 Actions 在基类里，必须排在图元派生元素之前
    page0 = D + (
        '<ofd:Page xmlns:ofd="%s"><ofd:Area><ofd:PhysicalBox>0 0 210 297</ofd:PhysicalBox></ofd:Area>'
        '<ofd:Content><ofd:Layer ID="40">'
        # 等宽字型（表44 FixedWidth）
        '<ofd:TextObject ID="41" Boundary="20 20 150 12" Font="2" Size="6">'
        '<ofd:TextCode X="0" Y="8">等宽 ABC123</ofd:TextCode></ofd:TextObject>'
        # GotoA：打开附件 A1；Region 用 9.3 的非紧缩描述
        '<ofd:PathObject ID="42" Boundary="20 40 60 20" Fill="true" Stroke="false">'
        '<ofd:Actions><ofd:Action Event="CLICK"><ofd:Region>'
        '<ofd:Area Start="0 0"><ofd:Line Point1="60 0"/><ofd:Line Point1="60 20"/>'
        '<ofd:Line Point1="0 20"/><ofd:Close/></ofd:Area>'
        '</ofd:Region>'
        '<ofd:GotoA AttachID="A1" NewWindow="true"/></ofd:Action></ofd:Actions>'
        '<ofd:FillColor Value="200 220 240" ColorSpace="1"/>'
        '<ofd:AbbreviatedData>M 0 0 L 60 0 L 60 20 L 0 20 C</ofd:AbbreviatedData>'
        '</ofd:PathObject>'
        # URI 动作（带 Base）
        '<ofd:PathObject ID="43" Boundary="20 70 60 20" Fill="true" Stroke="false">'
        '<ofd:Actions><ofd:Action Event="CLICK"><ofd:URI Base="https://example.com/" URI="doc"/></ofd:Action></ofd:Actions>'
        '<ofd:FillColor Value="220 235 200" ColorSpace="1"/>'
        '<ofd:AbbreviatedData>M 0 0 L 60 0 L 60 20 L 0 20 C</ofd:AbbreviatedData>'
        '</ofd:PathObject>'
        # Goto（带目标区域与缩放）
        '<ofd:PathObject ID="44" Boundary="20 100 60 20" Fill="true" Stroke="false">'
        '<ofd:Actions><ofd:Action Event="CLICK"><ofd:Goto>'
        '<ofd:Dest Type="XYZ" PageID="20" Left="5" Top="6" Zoom="200"/>'
        '</ofd:Goto></ofd:Action></ofd:Actions>'
        '<ofd:FillColor Value="240 220 220" ColorSpace="1"/>'
        '<ofd:AbbreviatedData>M 0 0 L 60 0 L 60 20 L 0 20 C</ofd:AbbreviatedData>'
        '</ofd:PathObject>'
        # 音频 / 视频（只解析不出声）
        '<ofd:PathObject ID="45" Boundary="20 130 60 20" Fill="true" Stroke="false">'
        '<ofd:Actions>'
        '<ofd:Action Event="CLICK"><ofd:Sound ResourceID="500" Volume="60" Repeat="false"/></ofd:Action>'
        '<ofd:Action Event="CLICK"><ofd:Movie ResourceID="501" Operator="Play"/></ofd:Action>'
        '</ofd:Actions>'
        '<ofd:FillColor Value="235 235 235" ColorSpace="1"/>'
        '<ofd:AbbreviatedData>M 0 0 L 60 0 L 60 20 L 0 20 C</ofd:AbbreviatedData>'
        '</ofd:PathObject>'
        '</ofd:Layer></ofd:Content></ofd:Page>') % NS

    page1 = D + (
        '<ofd:Page xmlns:ofd="%s"><ofd:Area><ofd:PhysicalBox>0 0 210 297</ofd:PhysicalBox></ofd:Area>'
        '<ofd:Content><ofd:Layer ID="50">'
        '<ofd:TextObject ID="51" Boundary="20 20 150 12" Font="1" Size="6">'
        '<ofd:TextCode X="0" Y="8">第二页：书签的落点</ofd:TextCode></ofd:TextObject>'
        '</ofd:Layer></ofd:Content></ofd:Page>') % NS

    res = D + (
        '<ofd:Res xmlns:ofd="%s" BaseLoc="Res">'
        '<ofd:ColorSpaces><ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/></ofd:ColorSpaces>'
        '<ofd:Fonts>'
        '<ofd:Font ID="1" FontName="SimSun" FamilyName="宋体"/>'
        # 表44：FixedWidth 表示等宽字型
        '<ofd:Font ID="2" FontName="Courier New" FamilyName="Courier New" FixedWidth="true"/>'
        '</ofd:Fonts>'
        '<ofd:MultiMedias>'
        '<ofd:MultiMedia ID="500" Type="Audio" Format="AVS"><ofd:MediaFile>a.avs</ofd:MediaFile></ofd:MultiMedia>'
        '<ofd:MultiMedia ID="501" Type="Video" Format="AVS"><ofd:MediaFile>v.avs</ofd:MediaFile></ofd:MultiMedia>'
        '</ofd:MultiMedias>'
        '</ofd:Res>') % NS

    # 表72/73：附件列表
    atts = D + (
        '<ofd:Attachments xmlns:ofd="%s">'
        '<ofd:Attachment ID="A1" Name="说明.txt" Format="TXT" Size="1" Visible="true" Usage="none">'
        '<ofd:FileLoc>Files/A1.txt</ofd:FileLoc></ofd:Attachment>'
        '<ofd:Attachment ID="A2" Name="台账.csv" Format="CSV" Size="1" ModDate="2026-01-02T03:04:05">'
        '<ofd:FileLoc>Files/A2.csv</ofd:FileLoc></ofd:Attachment>'
        '</ofd:Attachments>') % NS

    # 表63：自定义标引
    tags = D + (
        '<ofd:CustomTags xmlns:ofd="%s">'
        '<ofd:CustomTag NameSpace="http://example.com/tag/2026">'
        '<ofd:SchemaLoc>Tags/tag.xsd</ofd:SchemaLoc>'
        '<ofd:FileLoc>Tags/t1.xml</ofd:FileLoc></ofd:CustomTag>'
        '</ofd:CustomTags>') % NS

    # 表64/65：扩展信息
    exts = D + (
        '<ofd:Extensions xmlns:ofd="%s">'
        '<ofd:Extension AppName="小黄OFD" AppVersion="1.0.0" Company="小黄" RefId="21" Date="2026-10-30T00:00:00">'
        '<ofd:Property Name="来源" Type="string">样本</ofd:Property>'
        '</ofd:Extension></ofd:Extensions>') % NS

    # 表66：签名列表（附录 A：MaxSignId 是子元素）
    sigs = D + (
        '<ofd:Signatures xmlns:ofd="%s">'
        '<ofd:MaxSignId>s1</ofd:MaxSignId>'
        '<ofd:Signature ID="s1" Type="Seal" BaseLoc="Sign_0/Signature.xml"/>'
        '</ofd:Signatures>') % NS

    # 表67-69：签名描述（摘要、外观、签名值）——只解析，不验签
    # 元素/属性按附录 A 的 Signature.xsd：DateTime/Method 是元素，CheckValue 是元素，
    # SignedValue 的正文是 ST_Loc 文本。
    sig = D + (
        '<ofd:Signature xmlns:ofd="%s">'
        '<ofd:SignedInfo>'
        '<ofd:Provider ProviderName="SampleSigner" Company="小黄" Version="1.0"/>'
        '<ofd:SignatureMethod>SM3withSM2</ofd:SignatureMethod>'
        '<ofd:SignatureDateTime>2026-10-30T01:02:03</ofd:SignatureDateTime>'
        '<ofd:References CheckMethod="MD5">'
        '<ofd:Reference FileRef="/Doc_0/Document.xml"><ofd:CheckValue>AAAA</ofd:CheckValue></ofd:Reference>'
        '<ofd:Reference FileRef="/Doc_0/Pages/Page_0/Content.xml"><ofd:CheckValue>BBBB</ofd:CheckValue></ofd:Reference>'
        '</ofd:References>'
        '<ofd:StampAnnot PageRef="10" ID="s900" Boundary="120 200 40 40"/>'
        '<ofd:Seal><ofd:BaseLoc>Seal.esl</ofd:BaseLoc></ofd:Seal>'
        '</ofd:SignedInfo>'
        '<ofd:SignedValue>SignedValue.dat</ofd:SignedValue>'
        '</ofd:Signature>') % NS

    # 表70：版本列表已内嵌在 OFD.xml；这里只放两个版本描述文件（表71）
    ver1 = D + (
        '<ofd:DocVersion xmlns:ofd="%s" ID="v1" Version="1.0" Name="初版" CreationDate="2026-10-01">'
        '<ofd:FileList><ofd:File ID="f1">/Doc_0/Document.xml</ofd:File></ofd:FileList>'
        '<ofd:DocRoot>/Doc_0/Document.xml</ofd:DocRoot></ofd:DocVersion>') % NS
    ver2 = D + (
        '<ofd:DocVersion xmlns:ofd="%s" ID="v2" Version="1.0" Name="修订" CreationDate="2026-10-20">'
        '<ofd:FileList><ofd:File ID="f1">/Doc_0/Document.xml</ofd:File></ofd:FileList>'
        '<ofd:DocRoot>/Doc_0/Document.xml</ofd:DocRoot></ofd:DocVersion>') % NS

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("OFD.xml", ofd)
        z.writestr("Doc_0/Document.xml", doc)
        z.writestr("Doc_0/PublicRes.xml", res)
        z.writestr("Doc_0/Pages/Page_0/Content.xml", page0)
        z.writestr("Doc_0/Pages/Page_1/Content.xml", page1)
        z.writestr("Doc_0/Attachments.xml", atts)
        z.writestr("Doc_0/Files/A1.txt", "这是附件 A1 的内容。\n")
        z.writestr("Doc_0/Files/A2.csv", "日期,数量\n2026-10-30,1\n")
        z.writestr("Doc_0/CustomTags.xml", tags)
        z.writestr("Doc_0/Tags/t1.xml", D + '<t xmlns="http://example.com/tag/2026">x</t>')
        z.writestr("Doc_0/Extensions.xml", exts)
        z.writestr("Signs/Signatures.xml", sigs)
        z.writestr("Signs/Sign_0/Signature.xml", sig)
        z.writestr("Signs/Sign_0/SignedValue.dat", b"\x00\x01binary")
        z.writestr("Doc_0/V1/DocVersion.xml", ver1)
        z.writestr("Doc_0/V2/DocVersion.xml", ver2)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=os.path.join(ROOT, "out", "extras_test.ofd"))
    args = ap.parse_args(argv)
    path = build(args.out)
    print("已写出 %s（%d 字节）" % (path, os.path.getsize(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

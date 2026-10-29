# -*- coding: utf-8 -*-
"""生成符合 GB/T 33190-2016 结构的测试 OFD。

用途：自测、回归、给界面一个能打开的样本。不依赖任何第三方库。
    python tools/make_sample_ofd.py out/sample.ofd [--pages N] [--image]
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile
from xml.sax.saxutils import escape

NS = "http://www.ofdspec.org/2016"
DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'


def w(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def make_png(path, w_px=240, h_px=120, color=(0, 122, 204)):
    """用最朴素的办法生成 PNG：有 Pillow 就用，没有就手工写一个纯色 PNG。"""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (w_px, h_px), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, w_px - 1, h_px - 1], outline=color, width=3)
        d.ellipse([10, 10, h_px - 10, h_px - 10], fill=color)
        d.text((h_px, h_px // 2 - 6), "OFD SAMPLE", fill=(30, 30, 30))
        img.save(path, "PNG")
        return
    except Exception:
        pass
    import struct
    import zlib
    raw = b""
    for y in range(h_px):
        raw += b"\x00"
        for x in range(w_px):
            edge = x < 3 or y < 3 or x >= w_px - 3 or y >= h_px - 3
            raw += bytes((0, 122, 204)) if edge else bytes((255, 255, 255))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return c

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w_px, h_px, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def ofd_xml(doc_id: str, title: str, author: str) -> str:
    return DECL + (
        '<ofd:OFD xmlns:ofd="%s" DocType="OFD" Version="1.0">\n'
        '  <ofd:DocBody>\n'
        '    <ofd:DocInfo>\n'
        '      <ofd:DocID>%s</ofd:DocID>\n'
        '      <ofd:Title>%s</ofd:Title>\n'
        '      <ofd:Author>%s</ofd:Author>\n'
        '    </ofd:DocInfo>\n'
        '    <ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot>\n'
        '  </ofd:DocBody>\n'
        '</ofd:OFD>\n'
    ) % (NS, escape(doc_id), escape(title), escape(author))


def document_xml(page_ids, max_id: int, has_image: bool) -> str:
    pages = "\n".join(
        '      <ofd:Page ID="%d" BaseLoc="Pages/Page_%d/Content.xml"/>' % (pid, i)
        for i, pid in enumerate(page_ids)
    )
    doc_res = '    <ofd:DocumentRes>DocumentRes.xml</ofd:DocumentRes>\n' if has_image else ""
    return DECL + (
        '<ofd:Document xmlns:ofd="%s">\n'
        '  <ofd:CommonData>\n'
        '    <ofd:MaxUnitID>%d</ofd:MaxUnitID>\n'
        '    <ofd:PageArea>\n'
        '      <ofd:PhysicalBox>0 0 210 297</ofd:PhysicalBox>\n'
        '    </ofd:PageArea>\n'
        '    <ofd:PublicRes>PublicRes.xml</ofd:PublicRes>\n'
        '%s'
        '  </ofd:CommonData>\n'
        '  <ofd:Pages>\n'
        '%s\n'
        '  </ofd:Pages>\n'
        '</ofd:Document>\n'
    ) % (NS, max_id, doc_res, pages)


def public_res_xml() -> str:
    return DECL + (
        '<ofd:Res xmlns:ofd="%s" BaseLoc="Res">\n'
        '  <ofd:ColorSpaces>\n'
        '    <ofd:ColorSpace ID="1" Type="RGB" BitsPerComponent="8"/>\n'
        '    <ofd:ColorSpace ID="6" Type="GRAY" BitsPerComponent="8"/>\n'
        '  </ofd:ColorSpaces>\n'
        '  <ofd:Fonts>\n'
        '    <ofd:Font ID="2" FontName="SimSun" FamilyName="宋体" Charset="unicode"/>\n'
        '    <ofd:Font ID="3" FontName="SimHei" FamilyName="黑体" Charset="unicode"/>\n'
        '    <ofd:Font ID="4" FontName="Times New Roman" FamilyName="Times New Roman" Charset="unicode"/>\n'
        '  </ofd:Fonts>\n'
        '</ofd:Res>\n'
    ) % NS


def document_res_xml() -> str:
    return DECL + (
        '<ofd:Res xmlns:ofd="%s" BaseLoc="Res">\n'
        '  <ofd:MultiMedias>\n'
        '    <ofd:MultiMedia ID="20" Type="Image">\n'
        '      <ofd:MediaFile>logo.png</ofd:MediaFile>\n'
        '    </ofd:MultiMedia>\n'
        '  </ofd:MultiMedias>\n'
        '</ofd:Res>\n'
    ) % NS


def page0_content(has_image: bool) -> str:
    img = ('      <ofd:ImageObject ID="7" Boundary="20 232 60 30" ResourceID="20"'
           ' CTM="1 0 0 1 0 0"/>\n') if has_image else ""
    return DECL + (
        '<ofd:Page xmlns:ofd="%s">\n'
        '  <ofd:Area>\n'
        '    <ofd:PhysicalBox>0 0 210 297</ofd:PhysicalBox>\n'
        '  </ofd:Area>\n'
        '  <ofd:Content>\n'
        '    <ofd:Layer ID="5" Type="Body">\n'
        '      <ofd:TextObject ID="8" Boundary="20 22 170 16" Font="3" Size="9"'
        ' Weight="700" Fill="true">\n'
        '        <ofd:TextCode X="0" Y="12" Text="银河麒麟 OFD 处理软件"/>\n'
        '      </ofd:TextObject>\n'
        '      <ofd:TextObject ID="9" Boundary="20 44 170 8" Font="2" Size="4.5"'
        ' Fill="true">\n'
        '        <ofd:TextCode X="0" Y="6" Text="读写 · 编辑 · 打印   版本 1.0   目标平台 aarch64"/>\n'
        '      </ofd:TextObject>\n'
        '      <ofd:PathObject ID="10" Boundary="20 58 170 0.5" LineWidth="0.5"'
        ' Stroke="true">\n'
        '        <ofd:Path Data="M 0 0 L 170 0"/>\n'
        '      </ofd:PathObject>\n'
        '      <ofd:TextObject ID="11" Boundary="20 70 170 12" Font="2" Size="5"'
        ' Fill="true">\n'
        '        <ofd:TextCode X="0" Y="7" Text="一、这是自动生成的测试文档，包含文本、路径与图片对象。"/>\n'
        '      </ofd:TextObject>\n'
        '      <ofd:TextObject ID="12" Boundary="20 82 170 12" Font="2" Size="5"'
        ' Fill="true">\n'
        '        <ofd:TextCode X="0" Y="7" Text="二、文字可以直接在软件里改，改完另存仍然是标准 OFD。"/>\n'
        '      </ofd:TextObject>\n'
        '      <ofd:PathObject ID="13" Boundary="20 100 170 60" LineWidth="0.6"'
        ' Stroke="true">\n'
        '        <ofd:Path Data="M 0 0 L 170 0 L 170 60 L 0 60 Z"/>\n'
        '      </ofd:PathObject>\n'
        '      <ofd:PathObject ID="14" Boundary="20 100 170 60" LineWidth="0.3"'
        ' Stroke="true">\n'
        '        <ofd:Path Data="M 0 0 L 170 60 M 170 0 L 0 60 M 0 30 L 170 30"/>\n'
        '      </ofd:PathObject>\n'
        '      <ofd:PathObject ID="15" Boundary="90 150 60 24" LineWidth="0.4"'
        ' Fill="true" FillColor="232 244 255" Stroke="true" StrokeColor="0 122 204"'
        ' ColorSpace="1">\n'
        '        <ofd:Path Data="M 0 12 B 0 4 8 0 30 0 B 52 0 60 4 60 12'
        ' B 60 20 52 24 30 24 B 8 24 0 20 0 12 C"/>\n'
        '      </ofd:PathObject>\n'
        '      <ofd:TextObject ID="16" Boundary="90 150 60 24" Font="2" Size="4.5"'
        ' Fill="true">\n'
        '        <ofd:TextCode X="12" Y="15" Text="贝塞尔曲线"/>\n'
        '      </ofd:TextObject>\n'
        '%s'
        '    </ofd:Layer>\n'
        '  </ofd:Content>\n'
        '</ofd:Page>\n'
    ) % (NS, img)


def page1_content() -> str:
    """第二页：表格（列坐标对齐）+ 拉丁文字 + 字距 + 旋转文本。"""
    cols = [20.0, 60.0, 130.0, 190.0]      # 三条竖线的 x
    rows = [45.0, 65.0, 85.0, 105.0, 125.0]  # 四条横线 + 表格底
    body = []

    # 表格线
    d = "M 0 0 L %s 0 L %s %s L 0 %s Z" % (
        _n(cols[-1] - cols[0]), _n(cols[-1] - cols[0]),
        _n(rows[-1] - rows[0]), _n(rows[-1] - rows[0]))
    for y in rows[1:-1]:
        d += " M 0 %s L %s %s" % (_n(y - rows[0]), _n(cols[-1] - cols[0]), _n(y - rows[0]))
    for x in cols[1:-1]:
        d += " M %s 0 L %s %s" % (_n(x - cols[0]), _n(x - cols[0]), _n(rows[-1] - rows[0]))
    body.append(
        '      <ofd:PathObject ID="42" Boundary="%s %s %s %s" LineWidth="0.4"'
        ' Stroke="true">\n'
        '        <ofd:Path Data="%s"/>\n'
        '      </ofd:PathObject>\n'
        % (_n(cols[0]), _n(rows[0]), _n(cols[-1] - cols[0]), _n(rows[-1] - rows[0]), d))

    # 单元格文本：列宽 - 左右各 2mm 内边距，垂直居中（Y = (格高 + 字号) / 2）
    cells = [
        (0, 0, "序号"), (1, 0, "项目"), (2, 0, "说明"),
        (0, 1, "1"), (1, 1, "页面渲染"), (2, 1, "毫米坐标换算像素"),
        (0, 2, "2"), (1, 2, "文字编辑"), (2, 2, "改 TextCode 的 Text"),
        (0, 3, "3"), (1, 3, "打印输出"), (2, 3, "CUPS / Qt · N 合 1"),
    ]
    oid = 43
    for ci, ri, txt in cells:
        x = cols[ci] + 2.0
        w = (cols[ci + 1] - cols[ci]) - 4.0
        y = rows[ri] + 2.0
        h = (rows[ri + 1] - rows[ri]) - 4.0
        baseline = _n(4.0 + (h - 4.0) / 2 + 1.6)
        body.append(
            '      <ofd:TextObject ID="%d" Boundary="%s %s %s %s" Font="2" Size="4"'
            ' Fill="true">\n'
            '        <ofd:TextCode X="0" Y="%s" Text="%s"/>\n'
            '      </ofd:TextObject>\n'
            % (oid, _n(x), _n(y), _n(w), _n(h), baseline, txt))
        oid += 1

    # 拉丁文字 / 字距 / 旋转
    body.append(
        '      <ofd:TextObject ID="60" Boundary="20 150 170 12" Font="4" Size="4.5"'
        ' Fill="true">\n'
        '        <ofd:TextCode X="0" Y="8" Text="Latin text with Times New Roman:'
        ' The quick brown fox jumps over 13 lazy dogs."/>\n'
        '      </ofd:TextObject>\n')
    body.append(
        '      <ofd:TextObject ID="61" Boundary="20 170 170 12" Font="2" Size="5"'
        ' Fill="true">\n'
        '        <ofd:TextCode X="0" Y="8" Text="字距测试：每字加宽 2mm"'
        ' DeltaX="2 2 2 2 2 2 2 2 2 2"/>\n'
        '      </ofd:TextObject>\n')
    body.append(
        '      <ofd:TextObject ID="62" Boundary="24 200 60 14" Font="3" Size="5"'
        ' Fill="true" '
        ' CTM="0.7071 0.7071 -0.7071 0.7071 162.2 22.4">\n'
        '        <ofd:TextCode X="0" Y="6" Text="旋转 45 度"/>\n'
        '      </ofd:TextObject>\n')

    return DECL + (
        '<ofd:Page xmlns:ofd="%s">\n'
        '  <ofd:Area>\n'
        '    <ofd:PhysicalBox>0 0 210 297</ofd:PhysicalBox>\n'
        '  </ofd:Area>\n'
        '  <ofd:Content>\n'
        '    <ofd:Layer ID="40" Type="Body">\n'
        '      <ofd:TextObject ID="41" Boundary="20 22 170 14" Font="3" Size="8"'
        ' Weight="700" Fill="true">\n'
        '        <ofd:TextCode X="0" Y="11" Text="第二页：表格、字距与旋转"/>\n'
        '      </ofd:TextObject>\n'
        '%s'
        '    </ofd:Layer>\n'
        '  </ofd:Content>\n'
        '</ofd:Page>\n'
    ) % (NS, "".join(body))


def _n(v: float) -> str:
    """数值转字符串，去掉多余的 0。"""
    s = ("%.3f" % float(v)).rstrip("0").rstrip(".")
    return s if s else "0"


def build(out_path: str, pages: int = 2, with_image: bool = True) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    page_ids = [100 + i for i in range(pages)]
    max_id = 100 + pages + 30
    zf = zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED)

    def put(name, text):
        zf.writestr(name, text.encode("utf-8"))

    put("OFD.xml", ofd_xml("OFDLITE-TEST-0001", "OFD 测试文档", "ofdlite"))
    put("Doc_0/Document.xml", document_xml(page_ids, max_id, with_image))
    put("Doc_0/PublicRes.xml", public_res_xml())
    if with_image:
        put("Doc_0/DocumentRes.xml", document_res_xml())
        tmp = out_path + ".logo.png"
        make_png(tmp)
        with open(tmp, "rb") as fh:
            zf.writestr("Doc_0/Res/logo.png", fh.read())
        os.remove(tmp)
    put("Doc_0/Pages/Page_0/Content.xml", page0_content(with_image))
    for i in range(1, pages):
        put("Doc_0/Pages/Page_%d/Content.xml" % i, page1_content())
    zf.writestr("Doc_0/Attachments/", b"")
    zf.close()
    # 上面的模板用属性简写（FillColor="…" / Text="…" / Path Data="…"），
    # 标准 schema 只认子元素形式，输出前统一规范化（见 ofdlite/normalize.py）。
    try:
        import os as _os
        import sys as _sys
        _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from ofdlite.normalize import normalize_zip
        normalize_zip(out_path)
    except Exception as exc:
        print("规范化跳过（%s），产物可能不完全符合 schema" % exc)
    return out_path


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成测试 OFD")
    ap.add_argument("out", nargs="?", default="out/sample.ofd")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--no-image", action="store_true")
    args = ap.parse_args(argv)
    path = build(args.out, args.pages, not args.no_image)
    print("wrote", path, os.path.getsize(path), "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())


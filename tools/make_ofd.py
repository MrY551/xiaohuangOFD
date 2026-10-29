# -*- coding: utf-8 -*-
"""从 JSON 描述生成 OFD 文件（GB/T 33190-2016）。

和 make_sample_ofd.py 的区别：那个是内容写死的自测样本，这个能生成任意文档。

    python tools/make_ofd.py spec.json -o out/自定义.ofd
    python tools/make_ofd.py --print-spec          # 打印示例规格
    python tools/make_ofd.py spec.json --validate  # 生成后跑 XSD 校验

规格（全部单位 mm，坐标原点为页面左上角）：

    {
      "title": "文档标题",
      "author": "作者",
      "page": {"width": 210, "height": 297},
      "pages": [                        # 不写则用 page 生成 1 页
        {"objects": [ ... ]},
        {"objects": [ ... ]}
      ]
    }

对象类型：

    {"type": "text", "text": "标题", "x": 20, "y": 30, "size": 8,
     "font": "宋体", "bold": false, "italic": false,
     "fill": "#000000", "hscale": 1.0, "char_direction": 0, "read_direction": 0}

    {"type": "rect", "x": 20, "y": 50, "w": 170, "h": 40,
     "stroke": "#CC0000", "stroke_width": 0.3, "fill": null, "radius": 0}

    {"type": "line", "x1": 20, "y1": 100, "x2": 190, "y2": 100,
     "stroke": "#000000", "stroke_width": 0.2, "dash": [2, 1]}

    {"type": "ellipse", "x": 20, "y": 120, "w": 40, "h": 20,
     "stroke": "#0072C6", "stroke_width": 0.3, "fill": "#E8F4FF"}

    {"type": "image", "path": "logo.png", "x": 20, "y": 160, "w": 30, "h": 30}

    # 任意对象都可加 clip（组间交集、组内并集，坐标相对该对象 Boundary 左上角）：
    #   {"type": "rect", ..., "clip": [[[10,10,40,10,40,30,10,30], [...]]]}

    {"type": "path", "d": "M 0 0 L 40 0 L 40 20 C",
     "x": 20, "y": 200, "w": 40, "h": 20,
     "stroke": "#000000", "stroke_width": 0.3, "fill": null}

关于坐标：`x/y` 是对象在页面上的位置（Boundary 左上角）；`path.d` 里的坐标
是**相对 Boundary 左上角**的，从 (0,0) 开始——这是 GB/T 33190 的规定
（对象坐标原点在 Boundary 左上角），不是笔误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NS = "http://www.ofdspec.org/2016"
DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'

# 颜色空间 ID（PublicRes 里定义）
CS_RGB = "1"


# ------------------------------------------------------------------ 工具

def n(v: float) -> str:
    """数值格式化：去掉无意义的 .0，避免 Boundary 里出现 20.0 这种噪声。"""
    s = ("%.4f" % float(v)).rstrip("0").rstrip(".")
    return s or "0"


def hex_rgb(s: str):
    """'#RRGGBB' / '#RGB' / 'r,g,b' → (r, g, b)；None → None。"""
    if s is None:
        return None
    s = str(s).strip()
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    parts = [p.strip() for p in s.replace(" ", ",").split(",") if p.strip()]
    if len(parts) == 3:
        return tuple(int(float(p)) for p in parts)
    raise ValueError("看不懂的颜色: %r" % s)


def color_elem(tag: str, value) -> str:
    """颜色必须是子元素（schema 要求），不能写成属性。"""
    if value is None:
        return ""
    r, g, b = value
    return '<ofd:%s Value="%d %d %d" ColorSpace="%s"/>' % (tag, r, g, b, CS_RGB)


# ------------------------------------------------------------------ 各 XML

def ofd_xml(doc_id: str, title: str) -> str:
    return (DECL + '<ofd:OFD xmlns:ofd="%s" DocType="OFD" Version="1.0">\n'
            '  <ofd:DocBody>\n'
            '    <ofd:DocInfo>\n'
            '      <ofd:DocID>%s</ofd:DocID>\n'
            '      <ofd:Title>%s</ofd:Title>\n'
            '    </ofd:DocInfo>\n'
            '    <ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot>\n'
            '  </ofd:DocBody>\n'
            '</ofd:OFD>\n' % (NS, doc_id, escape(title)))


def public_res_xml(fonts, next_id: int) -> str:
    """fonts: [(id, name)]；颜色空间固定一个 RGB。"""
    parts = [DECL + '<ofd:Res xmlns:ofd="%s" BaseLoc="Res">\n' % NS]
    parts.append('  <ofd:ColorSpaces>\n')
    parts.append('    <ofd:ColorSpace ID="%s" Type="RGB" BitsPerComponent="8"/>\n' % CS_RGB)
    parts.append('  </ofd:ColorSpaces>\n')
    if fonts:
        parts.append('  <ofd:Fonts>\n')
        for fid, name in fonts:
            parts.append('    <ofd:Font ID="%s" FontName="%s" FamilyName="%s"/>\n'
                         % (fid, escape(name), escape(name)))
        parts.append('  </ofd:Fonts>\n')
    parts.append('</ofd:Res>\n')
    return "".join(parts)


def document_res_xml(images, next_id: int) -> str:
    """images: [(id, filename)]"""
    parts = [DECL + '<ofd:Res xmlns:ofd="%s" BaseLoc="Res">\n' % NS]
    if images:
        parts.append('  <ofd:MultiMedias>\n')
        for mid, fn in images:
            # 附录 A：MultiMedia 是 element-only，路径必须写在 <MediaFile> 子元素里，
            # 直接写成元素内容会被 schema 拒（Missing child element MediaFile）。
            parts.append(
                '    <ofd:MultiMedia ID="%s" Type="Image" Format="PNG">'
                '<ofd:MediaFile>%s</ofd:MediaFile></ofd:MultiMedia>\n'
                % (mid, escape(fn)))
        parts.append('  </ofd:MultiMedias>\n')
    parts.append('</ofd:Res>\n')
    return "".join(parts)


def text_xml(oid: int, o: dict, font_id: int) -> str:
    x = float(o.get("x", 0))
    y = float(o.get("y", 0))
    size = float(o.get("size", 5))
    text = str(o.get("text", ""))
    # Boundary 宽度给足：中文按 1em、拉丁按 0.55em 估，再加余量
    est = 0.0
    for ch in text:
        est += size if ord(ch) > 0x2E80 else size * 0.55
    w = float(o.get("w") or max(est * 1.08, size))
    h = float(o.get("h") or size * 1.6)

    attrs = ['ID="%d"' % oid,
             'Boundary="%s %s %s %s"' % (n(x), n(y), n(w), n(h)),
             'Font="%d"' % font_id,
             'Size="%s"' % n(size)]
    if o.get("bold"):
        attrs.append('Weight="700"')
    if o.get("italic"):
        attrs.append('Italic="true"')
    if o.get("hscale") is not None:
        attrs.append('HScale="%s"' % n(o["hscale"]))
    if o.get("char_direction"):
        attrs.append('CharDirection="%s"' % n(o["char_direction"]))
    if o.get("read_direction"):
        attrs.append('ReadDirection="%s"' % n(o["read_direction"]))
    fill = hex_rgb(o.get("fill", "#000000"))
    stroke = hex_rgb(o.get("stroke"))
    if stroke is None:
        attrs.append('Stroke="false"')

    # 基线在 Boundary 内的位置：一个字号往下，约为首行基线
    baseline = size if o.get("char_direction") in (None, 0) else size
    inner = [color_elem("FillColor", fill) if fill else "",
             color_elem("StrokeColor", stroke) if stroke else "",
             '<ofd:TextCode X="0" Y="%s">%s</ofd:TextCode>' % (n(baseline), escape(text))]
    return ('<ofd:TextObject %s>\n      %s\n    </ofd:TextObject>\n'
            % (" ".join(attrs), "".join(inner)))


def _clips_xml(groups) -> str:
    """把规格里的 clip（组列表，每组是若干坐标数组）写成 <ofd:Clips>。

    规格格式：[[[x,y,x,y,...], ...], ...]
      外层每个元素是一个 Clip（**组间取交集**），
      内层是若干子路径（**组内取并集**）。
    坐标相对对象 Boundary 左上角；每个 Area 的 Path 自带 Boundary（必选），
    取其子路径的包围盒。
    """
    if not groups:
        return ""
    parts = ["<ofd:Clips>"]
    for g in groups:
        parts.append("<ofd:Clip>")
        for pts in g:
            if len(pts) < 6:
                continue
            xs = [pts[k] for k in range(0, len(pts), 2)]
            ys = [pts[k] for k in range(1, len(pts), 2)]
            bx, by = min(xs), min(ys)
            bw = max(max(xs) - bx, 0.01)
            bh = max(max(ys) - by, 0.01)
            d = ["M %s %s" % (n(pts[0] - bx), n(pts[1] - by))]
            for k in range(2, len(pts) - 1, 2):
                d.append("L %s %s" % (n(pts[k] - bx), n(pts[k + 1] - by)))
            d.append("C")
            parts.append('<ofd:Area><ofd:Path Boundary="%s %s %s %s">'
                         '<ofd:AbbreviatedData>%s</ofd:AbbreviatedData>'
                         '</ofd:Path></ofd:Area>'
                         % (n(bx), n(by), n(bw), n(bh), " ".join(d)))
        parts.append("</ofd:Clip>")
    parts.append("</ofd:Clips>")
    return "".join(parts)


def path_xml(oid: int, o: dict, boundary, d: str) -> str:
    attrs = ['ID="%d"' % oid,
             'Boundary="%s %s %s %s"' % tuple(n(v) for v in boundary)]
    fill = hex_rgb(o.get("fill"))
    stroke = hex_rgb(o.get("stroke", "#000000"))
    lw = float(o.get("stroke_width", 0.25))

    # 表35：PathObject 的 Fill 默认 false、Stroke 默认 true。
    # 所以「有填充色」时必须**显式写 Fill="true"**，否则有颜色也不填充（会画成空白）。
    if fill is None:
        attrs.append('Fill="false"')
    else:
        attrs.append('Fill="true"')
    if stroke is None:
        attrs.append('Stroke="false"')
    else:
        attrs.append('Stroke="true"')
        attrs.append('LineWidth="%s"' % n(lw))
    inner = []
    # Clips 属于基类 CT_GraphicUnit 的 sequence，**必须排在派生类元素之前**
    # （XSD 的 extension 语义：基类元素在前）。写到 AbbreviatedData 后面会被拒。
    clip_xml = _clips_xml(o.get("clip"))
    if clip_xml:
        inner.append(clip_xml)
    if stroke is not None:
        inner.append(color_elem("StrokeColor", stroke))
    if fill is not None:
        inner.append(color_elem("FillColor", fill))
    if stroke is not None and o.get("dash"):
        # DashPattern 是 CT_GraphicUnit 的属性（ST_Array），写进属性列表，
        # 不能当子元素——schema 里 CT_Path 的 sequence 只允许
        # StrokeColor / FillColor / AbbreviatedData 三个子元素
        attrs.append('DashPattern="%s"' % " ".join(n(v) for v in o["dash"]))
    inner.append('<ofd:AbbreviatedData>%s</ofd:AbbreviatedData>' % d)
    return ('<ofd:PathObject %s>\n      %s\n    </ofd:PathObject>\n'
            % (" ".join(attrs), "".join(inner)))


def rect_d(w: float, h: float, radius: float = 0.0) -> str:
    """矩形路径（相对 Boundary）。圆角用 A 弧指令，四角各一段。"""
    if radius <= 0:
        return "M 0 0 L %s 0 L %s %s L 0 %s C" % (n(w), n(w), n(h), n(h))
    r = min(radius, w / 2.0, h / 2.0)
    return ("M %s 0 L %s 0 A %s %s 0 0 1 %s %s L %s %s "
            "A %s %s 0 0 1 %s %s L %s %s A %s %s 0 0 1 %s %s "
            "L 0 %s A %s %s 0 0 1 %s 0 C") % (
        n(r), n(w - r), n(r), n(r), n(w), n(r),
        n(w), n(h - r),
        n(r), n(r), n(w - r), n(h),
        n(r), n(h),
        n(r), n(r), n(0), n(h - r),
        n(r),
        n(r), n(r), n(r))


def ellipse_d(w: float, h: float) -> str:
    """椭圆路径：两段半圆弧（A 指令，rx/ry 不同即为椭圆）。"""
    rx, ry = w / 2.0, h / 2.0
    return ("M 0 %s A %s %s 0 0 1 %s %s A %s %s 0 0 1 0 %s C") % (
        n(ry), n(rx), n(ry), n(w), n(ry), n(rx), n(ry), n(ry))


def image_xml(oid: int, o: dict, media_id: int) -> str:
    x = float(o.get("x", 0))
    y = float(o.get("y", 0))
    w = float(o["w"])
    h = float(o["h"])
    return ('<ofd:ImageObject ID="%d" Boundary="%s %s %s %s" '
            'ResourceID="%d" CTM="%s 0 0 %s 0 0"/>\n'
            % (oid, n(x), n(y), n(w), n(h), media_id, n(w), n(h)))


# ------------------------------------------------------------------ 构建

def build_page(objects, ids, fonts, media_map):
    """把一页的对象列表翻译成 Content.xml 的图层片段。返回 (xml, 用到的字体)。"""
    body = []
    used_fonts = []

    for o in objects:
        t = o.get("type", "text")
        oid = ids()

        if t == "text":
            fname = o.get("font") or "宋体"
            if fname not in used_fonts:
                used_fonts.append(fname)
            fid = fonts[fname]
            body.append(text_xml(oid, o, fid))

        elif t == "rect":
            w = float(o.get("w", 10))
            h = float(o.get("h", 10))
            d = rect_d(w, h, float(o.get("radius", 0)))
            body.append(path_xml(oid, o, (o.get("x", 0), o.get("y", 0), w, h), d))

        elif t == "ellipse":
            w = float(o.get("w", 10))
            h = float(o.get("h", 10))
            body.append(path_xml(oid, o, (o.get("x", 0), o.get("y", 0), w, h),
                                 ellipse_d(w, h)))

        elif t == "line":
            x1, y1 = float(o.get("x1", 0)), float(o.get("y1", 0))
            x2, y2 = float(o.get("x2", 0)), float(o.get("y2", 0))
            # 线没有面积，Boundary 退化成两点包围盒
            bx, by = min(x1, x2), min(y1, y2)
            bw = max(abs(x2 - x1), 0.01)
            bh = max(abs(y2 - y1), 0.01)
            d = "M %s %s L %s %s" % (n(x1 - bx), n(y1 - by), n(x2 - bx), n(y2 - by))
            body.append(path_xml(oid, o, (bx, by, bw, bh), d))

        elif t == "image":
            path = o.get("path") or ""
            if not path:
                raise ValueError("image 对象缺少 path")
            media_id = media_map[os.path.basename(path)]
            body.append(image_xml(oid, o, media_id))

        elif t == "path":
            w = float(o.get("w", 10))
            h = float(o.get("h", 10))
            d = str(o.get("d", "M 0 0 L 1 0"))
            body.append(path_xml(oid, o, (o.get("x", 0), o.get("y", 0), w, h), d))

        else:
            raise ValueError("不认识的对象类型: %r" % t)

    content = (DECL
               + '<ofd:Page xmlns:ofd="%s">\n' % NS
               + '  <ofd:Area>\n'
               + '    <ofd:PhysicalBox>0 0 %s %s</ofd:PhysicalBox>\n' % (build_page.W, build_page.H)
               + '  </ofd:Area>\n'
               + '  <ofd:Content>\n'
               + '    <ofd:Layer ID="%d">\n      %s\n    </ofd:Layer>\n'
                 % (ids(), "".join(body))
               + '  </ofd:Content>\n'
               + '</ofd:Page>\n')
    return content, used_fonts


def make(spec: dict, out_path: str) -> str:
    page = spec.get("page") or {}
    W = float(page.get("width", 210))
    H = float(page.get("height", 297))
    build_page.W, build_page.H = W, H

    pages = spec.get("pages")
    if not pages:
        pages = [{"objects": spec.get("objects", [])}]

    # 收集图片
    images = []
    media_map = {}
    for pg in pages:
        for o in pg.get("objects", []):
            if o.get("type") == "image":
                p = o.get("path")
                if p and os.path.basename(p) not in media_map:
                    media_map[os.path.basename(p)] = None
                    images.append(p)

    # 分配 ID：1 给页对象之后的资源，从 2 开始；图片是 MultiMedia，也占 ID
    seq = [2]
    def next_id():
        v = seq[0]
        seq[0] += 1
        return v

    # 预留图片 ID
    media_ids = {}
    for p in images:
        media_ids[os.path.basename(p)] = next_id()

    font_table = {}
    page_xmls = []
    fonts_seen = []

    # 先扫一遍字体，保证 PublicRes 与 TextObject 的 Font 引用一致
    for pg in pages:
        for o in pg.get("objects", []):
            if o.get("type", "text") == "text":
                fname = o.get("font") or "宋体"
                if fname not in font_table:
                    font_table[fname] = next_id()

    for pg in pages:
        xml, _used = build_page(pg.get("objects", []), next_id, font_table,
                                media_ids)
        page_xmls.append(xml)

    page_count = len(page_xmls)
    doc_id = "xiaohuang-ofd"

    # Document.xml
    page_refs = "".join(
        '      <ofd:Page ID="%d" BaseLoc="Pages/Page_%d/Content.xml"/>\n' % (i + 100, i)
        for i in range(page_count))
    doc = (DECL + '<ofd:Document xmlns:ofd="%s">\n' % NS
           + '  <ofd:CommonData>\n'
           + '    <ofd:MaxUnitID>%d</ofd:MaxUnitID>\n' % (seq[0])
           + '    <ofd:PageArea>\n'
           + '      <ofd:PhysicalBox>0 0 %s %s</ofd:PhysicalBox>\n' % (n(W), n(H))
           + '    </ofd:PageArea>\n'
           + '    <ofd:PublicRes>PublicRes.xml</ofd:PublicRes>\n'
           + '    <ofd:DocumentRes>DocumentRes.xml</ofd:DocumentRes>\n'
           + '  </ofd:CommonData>\n'
           + '  <ofd:Pages>\n' + page_refs + '  </ofd:Pages>\n'
           + '</ofd:Document>\n')

    font_list = [(fid, name) for name, fid in font_table.items()]
    img_list = [(media_ids[k], k) for k in media_ids]

    # 打包
    tmp = out_path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("OFD.xml", ofd_xml(doc_id, spec.get("title", "小黄OFD 生成文档")))
        z.writestr("Doc_0/Document.xml", doc)
        z.writestr("Doc_0/PublicRes.xml", public_res_xml(font_list, seq[0]))
        z.writestr("Doc_0/DocumentRes.xml", document_res_xml(img_list, seq[0]))
        for i, xml in enumerate(page_xmls):
            z.writestr("Doc_0/Pages/Page_%d/Content.xml" % i, xml)
        for p in images:
            z.write(p, "Doc_0/Res/" + os.path.basename(p))
    os.replace(tmp, out_path)

    # 写出前统一规范化成 schema 允许的形式（复用主库的实现）
    try:
        from ofdlite.normalize import normalize_zip
        normalize_zip(out_path)
    except Exception as exc:      # 规范化失败不应静默，但也不该毁掉产物
        print("  [警告] 规范化未执行: %s" % exc)

    return out_path


SAMPLE_SPEC = {
    "title": "小黄OFD 生成示例",
    "author": "小黄",
    "page": {"width": 210, "height": 297},
    "pages": [{
        "objects": [
            {"type": "text", "text": "小黄OFD 生成示例", "x": 20, "y": 24,
             "size": 8, "font": "宋体", "bold": True},
            {"type": "text", "text": "从 JSON 描述生成，符合 GB/T 33190-2016",
             "x": 20, "y": 38, "size": 4.5, "font": "宋体"},
            {"type": "line", "x1": 20, "y1": 46, "x2": 190, "y2": 46,
             "stroke": "#0072C6", "stroke_width": 0.5},
            {"type": "rect", "x": 20, "y": 56, "w": 170, "h": 30,
             "stroke": "#CC0000", "stroke_width": 0.3, "radius": 2},
            {"type": "text", "text": "圆角矩形 · 描边 0.3mm · 无填充",
             "x": 24, "y": 70, "size": 4, "font": "宋体", "fill": "#333333"},
            {"type": "ellipse", "x": 20, "y": 96, "w": 50, "h": 24,
             "stroke": "#0072C6", "stroke_width": 0.4, "fill": "#E8F4FF"},
            {"type": "text", "text": "椭圆：贝塞尔四段近似", "x": 24, "y": 110,
             "size": 3.6, "font": "宋体"},
            {"type": "line", "x1": 20, "y1": 132, "x2": 190, "y2": 132,
             "stroke": "#666666", "stroke_width": 0.25, "dash": [2, 1]},
            {"type": "text", "text": "虚线（DashPattern）", "x": 20, "y": 138,
             "size": 3.6, "font": "宋体", "fill": "#666666"},
        ]
    }]
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", nargs="?", help="JSON 规格文件")
    ap.add_argument("-o", "--out", default="", help="输出 .ofd 路径")
    ap.add_argument("--print-spec", action="store_true", help="打印示例规格")
    ap.add_argument("--validate", action="store_true", help="生成后跑 XSD 校验")
    args = ap.parse_args(argv)

    if args.print_spec or not args.spec:
        print(json.dumps(SAMPLE_SPEC, ensure_ascii=False, indent=2))
        if not args.spec:
            return 0
        return 0

    spec = json.load(open(args.spec, encoding="utf-8"))
    out = args.out or os.path.splitext(args.spec)[0] + ".ofd"
    path = make(spec, out)
    print("已生成 %s（%d 字节）" % (path, os.path.getsize(path)))

    if args.validate:
        from ofdlite import OFDPackage
        from ofdlite.schema import SchemaSet
        ss = SchemaSet()
        if not ss.is_available():
            print("  XSD 不可用: %s" % ss.unavailable_reason())
            return 1
        pkg = OFDPackage.open(path)
        report = ss.validate_package(pkg)
        ok = sum(1 for st, _ in report.values() if st == "ok")
        bad = sum(1 for st, _ in report.values() if st == "fail")
        skip = sum(1 for st, _ in report.values() if st == "unavailable")
        print("  XSD: 通过 %d / 失败 %d / 跳过 %d" % (ok, bad, skip))
        for name, (st, errs) in sorted(report.items()):
            if st == "fail":
                print("   ", name, (errs[0] if errs else "")[:140])
        return 0 if bad == 0 else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

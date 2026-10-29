# -*- coding: utf-8 -*-
"""把内部简写形式的页面 XML 规范化成 GB/T 33190 附录 A 要求的形式。

为什么需要它：标准 schema 对写法有硬约束，而内部 DSL / 手写模板为了简洁
会用属性简写。两者不等价：

    内部简写                                  schema 要求
    <TextObject FillColor="0 0 0" ColorSpace="1">   <TextObject>
                                                        <FillColor Value="0 0 0" ColorSpace="1"/>
    <TextCode Text="abc"/>                          <TextCode>abc</TextCode>
    <Path Data="M 0 0 L 10 0"/>                     <AbbreviatedData>M 0 0 L 10 0</AbbreviatedData>

子元素顺序也是固定的（附录 A 的 CT_Path / CT_Text）：

    CT_Path:  StrokeColor → FillColor → AbbreviatedData
    CT_Text:  FillColor → StrokeColor → CGTransform → TextCode

顺序错了 lxml 会直接报 "This element is not expected"——真实数电发票就是
把 FillColor/StrokeColor 写在 AbbreviatedData 前面，属于它在这一点上不合规。

用法：

    from ofdlite.normalize import normalize_content
    fixed = normalize_content(raw_bytes)

依赖 lxml；没装时原样返回并给出提示（调用方决定要不要紧）。
"""
from __future__ import annotations

from typing import List, Optional

try:
    from lxml import etree
except Exception:          # pragma: no cover
    etree = None

NS = "http://www.ofdspec.org/2016"

# 附录 A 里各对象的子元素顺序（只列本模块会动的部分）
#
# 注意：Actions 与 Clips 属于基类 CT_GraphicUnit 的 sequence，在派生类的元素**之前**。
# XSD 的 extension 语义是"基类元素在前、派生类元素在后"，把 Clips 写在
# AbbreviatedData 后面会直接报 "Clips: This element is not expected"。
CT_GU_UNIT_HEAD = ["Actions", "Clips"]

ORDER_PREFIX = {
    "PathObject": CT_GU_UNIT_HEAD + ["StrokeColor", "FillColor", "AbbreviatedData"],
    "TextObject": CT_GU_UNIT_HEAD + ["FillColor", "StrokeColor", "CGTransform", "TextCode"],
    "ImageObject": CT_GU_UNIT_HEAD,
    "CompositeObject": CT_GU_UNIT_HEAD,
}

COLOR_ATTRS = ("FillColor", "StrokeColor", "ColorSpace",
               "FillColorSpace", "StrokeColorSpace")


def _q(tag: str) -> str:
    return "{%s}%s" % (NS, tag)


def _local(el) -> str:
    try:
        return etree.QName(el).localname
    except Exception:
        return ""


def _make_color(tag: str, value: str, cs: Optional[str]):
    el = etree.Element(_q(tag))
    el.set("Value", value)
    if cs:
        el.set("ColorSpace", cs)
    return el


def _fix_colors(el, order: List[str]) -> None:
    """对象上的颜色属性 → 子元素，并按 schema 顺序插到最前面。"""
    stroke = el.get("StrokeColor")
    fill = el.get("FillColor")
    cs = el.get("ColorSpace")
    changed = False
    for a in COLOR_ATTRS:
        if a in el.attrib:
            del el.attrib[a]
            changed = True
    if not changed or (not stroke and not fill):
        return
    wanted = {"StrokeColor": stroke, "FillColor": fill}
    children = []
    for tag in order:
        val = wanted.get(tag)
        if val:
            children.append(_make_color(tag, val, cs))
    for i, c in enumerate(children):
        el.insert(i, c)


def _fix_textcode(el) -> None:
    """Text 属性 → 元素内容（表46 只允许元素内容承载文字）。"""
    t = el.get("Text")
    if t is None:
        return
    del el.attrib["Text"]
    el.text = t


def _fix_path_element(el) -> None:
    """<Path Data="…"/> → <AbbreviatedData>…</AbbreviatedData>。"""
    d = el.get("Data")
    if d is None:
        return
    parent = el.getparent()
    if parent is None:
        return
    ab = etree.Element(_q("AbbreviatedData"))
    ab.text = d
    parent.replace(el, ab)


def _reorder(el, prefix: List[str]) -> None:
    """按 schema 顺序把前缀子元素挪到最前，其余保持原相对顺序。"""
    kids = list(el)
    if not kids:
        return
    buckets = {name: [] for name in prefix}
    rest = []
    for ch in kids:
        name = _local(ch)
        (buckets.get(name) or rest).append(ch) if name in buckets else rest.append(ch)
    ordered = []
    for name in prefix:
        ordered.extend(buckets.get(name, []))
    ordered.extend(rest)
    if ordered == kids:
        return
    for ch in kids:
        el.remove(ch)
    for ch in ordered:
        el.append(ch)


def normalize_content(data: bytes) -> bytes:
    """返回规范化后的 InContent XML 字节。"""
    if etree is None:
        return data
    try:
        root = etree.fromstring(data)
    except Exception:
        return data
    for el in list(root.iter()):
        tag = _local(el)
        if tag in ORDER_PREFIX:
            _fix_colors(el, ORDER_PREFIX[tag])
            _reorder(el, ORDER_PREFIX[tag])
        elif tag == "TextCode":
            _fix_textcode(el)
        elif tag == "Path":
            _fix_path_element(el)
    # 替换 Path 之后可能又乱了顺序，再走一遍
    for el in list(root.iter()):
        tag = _local(el)
        if tag in ORDER_PREFIX:
            _reorder(el, ORDER_PREFIX[tag])
    # 插入的子元素没有 tail，缩进会塌；统一重排一次
    try:
        etree.indent(root, space="  ")
    except Exception:
        pass
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                          pretty_print=True)


def normalize_zip(path: str, entries: Optional[List[str]] = None) -> int:
    """就地规范化 OFD 包内的 Content.xml。返回处理条目数。"""
    import zipfile
    if etree is None:
        return 0
    zin = zipfile.ZipFile(path, "r")
    items = [(i, zin.read(i.filename)) for i in zin.infolist()]
    zin.close()
    touched = 0
    out = []
    for info, data in items:
        name = info.filename
        is_content = name.endswith("Content.xml") and (
            entries is None or name in entries)
        if is_content:
            fixed = normalize_content(data)
            if fixed != data:
                touched += 1
            out.append((name, fixed, info.compress_type))
        else:
            out.append((name, data, info.compress_type))
    zout = zipfile.ZipFile(path, "w")
    for name, data, ctype in out:
        zout.writestr(name, data, compress_type=ctype)
    zout.close()
    return touched

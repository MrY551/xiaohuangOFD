# -*- coding: utf-8 -*-
"""XML 读写辅助。

OFD 的命名空间固定是 http://www.ofdspec.org/2016，但真实文件里经常出现：
  * 没有命名空间（部分国产工具导出的简化件）
  * 混入自定义命名空间（ofd:xxx、签名扩展）
  * 前缀不统一（ns0、ofd、无前缀）

策略：解析时按 local name 匹配（忽略命名空间），保存时统一写默认命名空间。
这样读得进各种野文件，写出来的又是标准件。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Iterable, Optional

OFD_NS = "http://www.ofdspec.org/2016"

# 写回时的前缀注册：默认命名空间 + 常见扩展
ET.register_namespace("", OFD_NS)
ET.register_namespace("ofd", OFD_NS)

_XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'
_COMMENT_RE = re.compile(rb"<!--.*?-->", re.S)


def local(tag: str) -> str:
    """去掉 {namespace} 前缀，返回本地名。"""
    if tag is None:
        return ""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def parse(data: bytes) -> ET.Element:
    """解析 XML 字节。容忍 BOM 与头部注释。"""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    # ElementTree 不喜欢 DOCTYPE，剥掉
    m = re.search(rb"<!DOCTYPE[^>]*>", data)
    if m:
        data = data[: m.start()] + data[m.end():]
    return ET.fromstring(data)


def find(root: ET.Element, name: str) -> Optional[ET.Element]:
    """按本地名找第一个直接子元素。"""
    for ch in list(root):
        if local(ch.tag) == name:
            return ch
    return None


def find_all(root: ET.Element, name: str) -> list:
    """按本地名找全部直接子元素。"""
    return [ch for ch in list(root) if local(ch.tag) == name]


def find_path(root: ET.Element, *names: str) -> Optional[ET.Element]:
    """按本地名逐层下钻，例如 find_path(root, 'CommonData', 'PageArea')。"""
    cur = root
    for n in names:
        nxt = find(cur, n)
        if nxt is None:
            return None
        cur = nxt
    return cur


def ensure_path(root: ET.Element, *names: str) -> ET.Element:
    """逐层下钻，缺失的层级按默认命名空间创建。"""
    cur = root
    for n in names:
        nxt = find(cur, n)
        if nxt is None:
            nxt = ET.SubElement(cur, "{%s}%s" % (OFD_NS, n))
        cur = nxt
    return cur


def deepfind_all(root: ET.Element, name: str) -> Iterable[ET.Element]:
    """整棵树里找所有同名元素（不区分命名空间）。"""
    for el in root.iter():
        if local(el.tag) == name:
            yield el


def tostring(root: ET.Element, with_decl: bool = True) -> bytes:
    """序列化为 UTF-8 字节，统一带 XML 声明。"""
    body = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    # ElementTree 在默认命名空间下会写成 xmlns="..."，这是我们要的
    if with_decl:
        return _XML_DECL.encode("utf-8") + body
    return body


def new_element(name: str, **attrs) -> ET.Element:
    """建一个带默认命名空间的元素。"""
    el = ET.Element("{%s}%s" % (OFD_NS, name))
    for k, v in attrs.items():
        if v is None:
            continue
        el.set(k, v if isinstance(v, str) else str(v))
    return el


def append_new(parent: ET.Element, name: str, **attrs) -> ET.Element:
    el = new_element(name, **attrs)
    parent.append(el)
    return el


def clone(el: ET.Element) -> ET.Element:
    """深拷贝元素（用于 undo 快照）。"""
    return ET.fromstring(ET.tostring(el, encoding="utf-8"))


def strip_ns(el: ET.Element) -> None:
    """把整棵树的命名空间去掉（导出成通用 XML 时用）。"""
    for e in el.iter():
        e.tag = local(e.tag)


# ---------------------------------------------------------------- 数值解析

def fnum(text, default: float = 0.0) -> float:
    """宽容地转 float。OFD 里偶见 "2.5mm"、空串、逗号分隔。"""
    if text is None:
        return default
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().replace(",", " ")
    if not s:
        return default
    try:
        return float(s.split()[0])
    except (ValueError, IndexError):
        m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s)
        return float(m.group()) if m else default


def fnum_list(text, n: Optional[int] = None, default: float = 0.0) -> list:
    """把 "1 2 3" / "1,2,3" 拆成 float 列表；不足 n 个用 default 补齐。"""
    if text is None:
        vals = []
    elif isinstance(text, (list, tuple)):
        vals = [fnum(v, default) for v in text]
    else:
        parts = re.split(r"[,\s]+", str(text).strip())
        vals = [fnum(p, default) for p in parts if p != ""]
    if n is not None:
        while len(vals) < n:
            vals.append(default)
        vals = vals[:n]
    return vals


def bool_attr(text, default: bool = False) -> bool:
    if text is None:
        return default
    return str(text).strip().lower() in ("true", "1", "yes")


def mm_to_pt(mm: float) -> float:
    return mm * 72.0 / 25.4


def pt_to_mm(pt: float) -> float:
    return pt * 25.4 / 72.0

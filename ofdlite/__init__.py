# -*- coding: utf-8 -*-
"""ofdlite —— OFD 文档处理核心库。

纯 Python 实现（标准库 + Pillow），不依赖 Qt/浏览器，桌面端与网页端共用。

    from ofdlite import OFDPackage
    pkg = OFDPackage.open("a.ofd")
    page = pkg.all_pages()[0]
    page.objects()          # 页面上所有对象
    pkg.save("b.ofd")       # 只重写改动过的 XML 条目
"""
from __future__ import annotations

def build_id(short: int = 12) -> str:
    """构建指纹，形如 1.0.0+g1a2b3c4d5e6f。懒加载，避免循环导入。"""
    from ofdlite.buildid import build_id as _b
    return _b(short)


__version__ = "1.0.0"

from .errors import (OfdError, PackageError, ParseError, PrintError,
                     RenderError, ResourceMissing)
from .geometry import Matrix, Rect
from .model import (CompositeObject, Document, DocumentRes, GenericObject,
                    ImageObject, Layer, OFDPackage, OFDObject, Page, PathObject,
                    PublicRes, TextObject, TextCode)
from .scene import PageScene, build_page_scene, page_outline, selection_boxes

__all__ = [
    "__version__",
    "build_id",
    "OfdError", "PackageError", "ParseError", "RenderError", "PrintError",
    "ResourceMissing",
    "Matrix", "Rect",
    "OFDPackage", "Document", "Page", "Layer", "OFDObject",
    "TextObject", "TextCode", "PathObject", "ImageObject", "CompositeObject",
    "GenericObject", "PublicRes", "DocumentRes",
    "PageScene", "build_page_scene", "page_outline", "selection_boxes",
    "open_ofd", "render_png", "to_pdf", "to_svg", "page_texts",
]


def open_ofd(path: str) -> OFDPackage:
    return OFDPackage.open(path)


def render_png(page, dpi: int = 150) -> bytes:
    from .render_pil import render_to_png
    from .scene import build_page_scene
    sc = build_page_scene(page)
    return render_to_png(sc, sc.resources, dpi=dpi)


def to_pdf(pkg: OFDPackage, path: str, dpi: int = 200) -> str:
    from .convert import export_pdf
    return export_pdf(pkg, path, dpi=dpi)


def to_svg(page, path: str = "") -> str:
    from .convert import export_svg
    return export_svg(page, path)


def page_texts(pkg: OFDPackage):
    out = []
    for page in pkg.all_pages():
        lines = []
        for obj in page.objects():
            if isinstance(obj, TextObject):
                t = obj.text()
                if t.strip():
                    lines.append(t)
        out.append(lines)
    return out

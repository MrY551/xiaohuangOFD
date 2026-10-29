# -*- coding: utf-8 -*-
"""OFD 数据模型。

设计原则：模型是 XML 元素的一层薄包装。
  * 读取时把 DOM 解析出来，模型对象只是"视图 + 便捷属性"
  * 编辑时直接改元素属性，未动的元素一个字节都不改
  * 保存时只重写被标记为脏的 XML 条目，其余 zip 条目按原字节回填

这样能保证：打开一个第三方 OFD、改一段文字、另存，除那段文字外其余字节完全不变，
签名以外的结构不被破坏。
"""
from __future__ import annotations

import io
import os
import zipfile
from typing import Dict, List, Optional, Sequence, Tuple

from . import colors as colorlib
from . import geometry as geo
from . import xmlutil as X
from .errors import ParseError, ResourceMissing

# 对象类型名 → 模型类，由 reader 注册
_OBJ_TYPES = {}
# 页对象类型名 → 模型类
_PAGE_TYPES = {}


def register_object(local_name: str):
    def deco(cls):
        _OBJ_TYPES[local_name] = cls
        return cls
    return deco


# ==================================================================== 包

class OFDPackage(object):
    """一个 .ofd 文件（其实是 zip 容器）。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.entries: Dict[str, bytes] = {}       # zip 条目原始字节
        self.dirty: set = set()                    # 需要重写的条目名
        self.xml_roots: Dict[str, "object"] = {}   # 条目名 → Element（已解析）
        self.ofd_root = None                       # OFD.xml 的根
        self.documents: List["Document"] = []
        self._entry_order: List[str] = []
        self._id_counter = 1000
        self.meta: Dict[str, str] = {}             # 文档元数据（作者/标题等）

    # ---------------------------------------------------------- 装载
    @classmethod
    def open(cls, path: str) -> "OFDPackage":
        pkg = cls(path)
        pkg._load_zip(path)
        pkg._parse_entry_structure()
        return pkg

    @classmethod
    def from_bytes(cls, data: bytes, name: str = "<memory>") -> "OFDPackage":
        pkg = cls(name)
        pkg._load_zip_bytes(data)
        pkg._parse_entry_structure()
        return pkg

    def _load_zip(self, path: str) -> None:
        if not os.path.exists(path):
            raise ParseError("文件不存在: %s" % path)
        with open(path, "rb") as fh:
            data = fh.read()
        self._load_zip_bytes(data)

    def _load_zip_bytes(self, data: bytes) -> None:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data), "r")
        except zipfile.BadZipFile as exc:
            raise ParseError("不是合法的 OFD/ZIP 容器: %s" % exc)
        with zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                try:
                    self.entries[name] = zf.read(info)
                except Exception as exc:  # 损坏条目：记下但不中断
                    raise ParseError("读取条目失败 %s: %s" % (name, exc))
                self._entry_order.append(name)

    def _parse_entry_structure(self) -> None:
        root_name = self._find_ofd_root_entry()
        if root_name is None:
            raise ParseError("包里找不到 OFD.xml")
        self.ofd_root = self.parse_xml(root_name)
        if self.ofd_root is None:
            raise ParseError("OFD.xml 解析失败")

        bodies = X.find_all(self.ofd_root, "DocBody")
        if not bodies:
            raise ParseError("OFD.xml 里没有 DocBody")
        for body in bodies:
            doc_root = X.find(body, "DocRoot")
            if doc_root is None or not (doc_root.text or "").strip():
                continue
            rel = (doc_root.text or "").strip().lstrip("/")
            doc = Document(self, rel)
            # 标题/作者优先从 OFD.xml 的 DocInfo 取（标准位置）
            info = X.find(body, "DocInfo")
            if info is not None:
                for tag, attr in (("Title", "title"), ("Author", "author"),
                                  ("Creator", "creator"), ("Subject", "subject")):
                    el = X.find(info, tag)
                    if el is not None and (el.text or "").strip():
                        setattr(doc, attr, el.text.strip())
            doc.load()
            self.documents.append(doc)
        if not self.documents:
            raise ParseError("OFD.xml 里没有可用的 DocRoot")

    def _find_ofd_root_entry(self) -> Optional[str]:
        for name in self.entries:
            if name.upper() == "OFD.XML" or name.upper().endswith("/OFD.XML"):
                return name
        # 有些包把入口放在子目录，按内容找
        for name in self.entries:
            if name.lower().endswith(".xml"):
                head = self.entries[name][:400]
                if b"<ofd:OFD" in head or b"<OFD" in head and b"DocBody" in self.entries[name][:2000]:
                    return name
        return None

    # ---------------------------------------------------------- 条目访问
    def has(self, name: str) -> bool:
        return name in self.entries

    def read(self, name: str) -> bytes:
        if name not in self.entries:
            raise ResourceMissing("包内缺少条目: %s" % name)
        return self.entries[name]

    def parse_xml(self, name: str):
        cached = self.xml_roots.get(name)
        if cached is not None:
            return cached
        if name not in self.entries:
            return None
        try:
            root = X.parse(self.entries[name])
        except Exception:
            return None
        self.xml_roots[name] = root
        return root

    def touch(self, name: str) -> None:
        """标记条目已修改，保存时重写。"""
        self.dirty.add(name)

    def add_entry(self, name: str, data: bytes) -> None:
        self.entries[name] = data
        self.dirty.add(name)
        if name not in self._entry_order:
            self._entry_order.append(name)

    def resolve(self, base_entry: str, href: str) -> str:
        """把相对引用解析成包内路径。href 可能是 "./Document.xml" 或 "../Res/x.jpg"。"""
        if not href:
            return ""
        href = href.replace("\\", "/").strip()
        if href.startswith("/"):
            return href.lstrip("/")
        base_dir = os.path.dirname(base_entry)
        joined = os.path.normpath(os.path.join(base_dir, href)).replace("\\", "/")
        return joined.lstrip("./") if joined.startswith("./") else joined

    def next_id(self) -> str:
        """分配一个新的对象 ID（包内唯一最大值 + 1）。"""
        used = 0
        for doc in self.documents:
            used = max(used, doc.max_unit_id)
        nid = max(used + 1, self._id_counter)
        self._id_counter = nid + 1
        for doc in self.documents:
            doc.max_unit_id = max(doc.max_unit_id, nid)
        return str(nid)

    # ---------------------------------------------------------- 保存
    def save(self, path: Optional[str] = None, keep_backup: bool = True) -> str:
        path = path or self.path
        if not path:
            raise ParseError("未指定保存路径")
        # 把标记为脏的 XML 树序列化回字节
        for name in list(self.dirty):
            root = self.xml_roots.get(name)
            if root is not None:
                data = X.tostring(root)
                # 写出去的页面内容要符合附录 A 的 schema：属性式颜色/文本、
                # <Path Data> 这些简写在标准里都不允许，见 normalize.py
                if name.endswith("Content.xml"):
                    try:
                        from .normalize import normalize_content
                        data = normalize_content(data)
                    except Exception:
                        pass
                self.entries[name] = data
        # 同步每份 Document 的 Pages / MaxUnitID
        for doc in self.documents:
            doc.flush_metadata()

        tmp = path + ".ofdlite.tmp"
        with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
            written = set()
            for name in self._entry_order:
                if name in self.entries and name not in written:
                    zf.writestr(name, self.entries[name])
                    written.add(name)
            for name, data in self.entries.items():
                if name not in written:
                    zf.writestr(name, data)
                    written.add(name)
        if keep_backup and os.path.exists(path):
            bak = path + ".bak"
            try:
                if os.path.exists(bak):
                    os.remove(bak)
                os.replace(path, bak)
            except OSError:
                pass
        os.replace(tmp, path)
        self.path = path
        self.dirty.clear()
        return path

    # ---------------------------------------------------------- 便捷访问
    @property
    def document(self) -> "Document":
        return self.documents[0]

    def page_count(self) -> int:
        return sum(len(d.pages) for d in self.documents)

    def all_pages(self) -> List["Page"]:
        out = []
        for d in self.documents:
            out.extend(d.pages)
        return out

    def find_page(self, page_id: str) -> Optional["Page"]:
        for d in self.documents:
            for p in d.pages:
                if p.id == page_id:
                    return p
        return None


# ==================================================================== 文档

class Document(object):
    """一份文档（Document.xml）。"""

    def __init__(self, pkg: OFDPackage, entry: str):
        self.pkg = pkg
        self.entry = entry
        self.root = None
        self.pages: List["Page"] = []
        self.max_unit_id = 0
        self.page_area: Optional[geo.Rect] = None   # 默认页尺寸（mm）
        self.public_res: List["PublicRes"] = []
        self.doc_res: Optional["DocumentRes"] = None
        self.templates: Dict[str, "TemplatePage"] = {}
        self.annotation_index: Dict[str, str] = {}
        self.annotations_entry: Optional[str] = None
        self.page_res_loaded: set = set()
        self.title = ""
        self.author = ""
        self.creator = ""
        self.subject = ""

    # ---------------------------------------------------------- 装载
    def load(self) -> None:
        root = self.pkg.parse_xml(self.entry)
        if root is None:
            raise ParseError("Document.xml 解析失败: %s" % self.entry)
        self.root = root

        common = X.find(root, "CommonData")
        if common is not None:
            self.max_unit_id = int(X.fnum(common.get("MaxUnitID"), 0))
            page_area = X.find(common, "PageArea")
            if page_area is not None:
                pb = X.find(page_area, "PhysicalBox")
                if pb is not None and (pb.text or "").strip():
                    self.page_area = geo.Rect.parse(pb.text)
            for tag in ("Title", "Author", "Creator", "Subject"):
                el = X.find(common, tag)
                if el is not None and (el.text or "").strip():
                    if tag == "Title":
                        self.title = el.text.strip()
                    elif tag == "Author":
                        self.author = el.text.strip()

        # 资源：标准放在 CommonData 里，也有工具直接挂在 Document 下，两处都收
        res_parents = []
        common_el = X.find(root, "CommonData")
        if common_el is not None:
            res_parents.append(common_el)
        res_parents.append(root)
        seen_res = set()
        for parent in res_parents:
            for pr in X.find_all(parent, "PublicRes"):
                href = (pr.text or "").strip()
                name = self.pkg.resolve(self.entry, href)
                if not href or name in seen_res:
                    continue
                seen_res.add(name)
                res = PublicRes(self, name)
                res.load()
                self.public_res.append(res)
            dr = X.find(parent, "DocumentRes")
            if dr is not None and self.doc_res is None:
                href = (dr.text or "").strip()
                name = self.pkg.resolve(self.entry, href)
                if href and name not in seen_res:
                    seen_res.add(name)
                    self.doc_res = DocumentRes(self, name)
                    self.doc_res.load()

        # 文档级模板页。数电发票、公文把票头/框线/水印放在这里，
        # 不加载就等于丢了整页的版式骨架。
        for pr in res_parents:
            for tp in X.find_all(pr, "TemplatePage"):
                tid = (tp.get("ID") or "").strip()
                if tid and tid not in self.templates:
                    self.templates[tid] = TemplatePage(self, tp)

        # 页树
        pages_el = X.find(root, "Pages")
        if pages_el is None:
            raise ParseError("Document.xml 里没有 Pages 节点")
        for pel in X.find_all(pages_el, "Page"):
            page = Page(self, pel)
            page.load_contents()
            self.pages.append(page)

        # 第15章 注释：CommonData/Annotations 指向注释列表，列表按页索引
        # 分页注释文件。签章（Stamp）常放在这里，不加载就"少个章"。
        self.annotation_index: Dict[str, str] = {}
        for parent in res_parents:
            ann = X.find(parent, "Annotations")
            if ann is None:
                continue
            href = (ann.text or "").strip()
            if not href:
                continue
            self.annotations_entry = self.pkg.resolve(self.entry, href)
            self.load_annotation_index()
            break
        for page in self.pages:
            page.load_annotations()

    def load_annotation_index(self) -> None:
        """读注释列表文件：Page(PageID, FileLoc)。

        表60：注释在列表文件里按页面组织索引，内容在分页注释文件里描述。
        """
        root = self.pkg.parse_xml(self.annotations_entry)
        if root is None:
            return
        for pel in X.deepfind_all(root, "Page"):
            pid = (pel.get("PageID") or "").strip()
            loc = (pel.get("FileLoc") or "").strip()
            if pid and loc:
                self.annotation_index[pid] = self.pkg.resolve(
                    self.annotations_entry, loc)

    # ---------------------------------------------------------- 资源查找
    def find_font(self, font_id: str):
        for res in self.public_res:
            f = res.fonts.get(str(font_id))
            if f is not None:
                return f
        return None

    def find_colorspace(self, cs_id: str):
        for res in self.public_res:
            cs = res.colorspaces.get(str(cs_id))
            if cs:
                return cs
        return None

    def find_drawparam(self, dp_id: str):
        """查 DrawParam。PublicRes 与 DocumentRes 两处都看。"""
        key = str(dp_id)
        for res in self.public_res:
            if key in res.drawparams:
                return res.drawparams[key]
        if self.doc_res is not None and key in self.doc_res.drawparams:
            return self.doc_res.drawparams[key]
        return None

    def find_image(self, res_id: str) -> Optional[Tuple[str, bytes]]:
        """返回 (文件名, 字节)。找不到返回 None。"""
        if self.doc_res is None:
            return None
        entry = self.doc_res.entry_of(str(res_id))
        if not entry or not self.pkg.has(entry):
            return None
        return (os.path.basename(entry), self.pkg.read(entry))

    # ---------------------------------------------------------- 页面操作
    def default_page_size(self) -> geo.Rect:
        if self.page_area is not None and not self.page_area.is_empty():
            return geo.Rect(self.page_area.x, self.page_area.y,
                            self.page_area.w, self.page_area.h)
        if self.pages:
            return self.pages[0].size()
        return geo.Rect(0, 0, 210, 297)   # A4

    def insert_page(self, index: int, size: Optional[geo.Rect] = None) -> "Page":
        """在 index 处插入空白页。"""
        size = size or self.default_page_size()
        pages_el = X.find(self.root, "Pages")
        if pages_el is None:
            pages_el = X.ensure_path(self.root, "Pages")
        # 新页的 BaseLoc 必须唯一
        used = set(p.base_loc.lower() for p in self.pages)
        n = len(self.pages) + 1
        while True:
            cand = "Pages/Page_%d/Content.xml" % n
            if cand.lower() not in used and not self.pkg.has(
                    self.pkg.resolve(self.entry, cand)):
                break
            n += 1

        page_id = self.pkg.next_id()
        pel = X.new_element("Page", ID=page_id, BaseLoc=cand)
        # 插入到 Pages 下正确的顺序位置
        children = list(pages_el)
        if index < len(children):
            pages_el.insert(index, pel)
        else:
            pages_el.append(pel)

        # 建 Content.xml
        content_entry = self.pkg.resolve(self.entry, cand)
        croot = X.new_element("Page")
        area = X.append_new(croot, "Area")
        X.append_new(area, "PhysicalBox").text = size.to_str()
        X.append_new(croot, "Content")
        self.pkg.add_entry(content_entry, X.tostring(croot))
        self.pkg.xml_roots[content_entry] = croot

        page = Page(self, pel)
        page.content_entry = content_entry
        page.content_root = croot
        page.load_contents()
        self.pages.insert(min(index, len(self.pages)), page)
        self.flush_metadata()
        return page

    def remove_page(self, index: int) -> bool:
        if index < 0 or index >= len(self.pages):
            return False
        page = self.pages.pop(index)
        pages_el = X.find(self.root, "Pages")
        if pages_el is not None:
            for ch in list(pages_el):
                if ch is page.element:
                    pages_el.remove(ch)
                    break
        self.pkg.touch(self.entry)
        self.flush_metadata()
        return True

    def move_page(self, src: int, dst: int) -> bool:
        if src == dst or not (0 <= src < len(self.pages)) or not (0 <= dst < len(self.pages)):
            return False
        page = self.pages.pop(src)
        self.pages.insert(dst, page)
        pages_el = X.find(self.root, "Pages")
        if pages_el is not None:
            for ch in list(pages_el):
                if X.local(ch.tag) == "Page":
                    pages_el.remove(ch)
            for p in self.pages:
                pages_el.append(p.element)
        self.pkg.touch(self.entry)
        self.flush_metadata()
        return True

    def flush_metadata(self) -> None:
        """把内存里的页序/ID 同步回 Document.xml 元素。"""
        if self.root is None:
            return
        common = X.find(self.root, "CommonData")
        if common is not None:
            common.set("MaxUnitID", str(max(self.max_unit_id, 1)))
        self.pkg.touch(self.entry)


# ==================================================================== 资源

class PublicRes(object):
    """PublicRes.xml：字体、色彩空间、绘制参数。"""

    def __init__(self, doc: Document, entry: str):
        self.doc = doc
        self.entry = entry
        self.root = None
        self.base_loc = ""
        self.fonts: Dict[str, "FontSpec"] = {}
        self.colorspaces: Dict[str, str] = {}
        self.drawparams: Dict[str, dict] = {}

    def load(self) -> None:
        root = self.doc.pkg.parse_xml(self.entry)
        if root is None:
            return
        self.root = root
        self.base_loc = (root.get("BaseLoc") or "").strip()
        # Font 在 Fonts 容器里，ColorSpace 在 ColorSpaces 里，用深度遍历兜住
        # 不同厂商把容器省掉直接挂 Font 的情况
        for fel in X.deepfind_all(root, "Font"):
            fid = fel.get("ID") or ""
            if not fid:
                continue
            self.fonts[fid] = FontSpec(
                fid,
                fel.get("FontName") or "",
                fel.get("FamilyName") or "",
                fel.get("Charset") or "",
                fel.get("Serif") or "",
                (fel.get("Bold") or "").lower() == "true",
                (fel.get("Italic") or "").lower() == "true",
                X.find(fel, "FontFile") is not None,
                self,
            )
        for cel in X.deepfind_all(root, "ColorSpace"):
            cid = cel.get("ID") or ""
            if cid:
                self.colorspaces[cid] = cel.get("Type") or cel.get("Name") or ""
        # DrawParam 的标准位置是 PublicRes/DrawParams，DocumentRes 里也可能有
        for dp in X.deepfind_all(root, "DrawParam"):
            did = dp.get("ID") or ""
            if did:
                self.drawparams[did] = parse_draw_param(dp)

    def font_file_entry(self, font: "FontSpec") -> Optional[str]:
        """如果字体有内嵌 FontFile，返回包内路径。"""
        if self.root is None:
            return None
        for fel in X.deepfind_all(self.root, "Font"):
            if fel.get("ID") == font.id:
                ff = X.find(fel, "FontFile")
                if ff is not None and (ff.text or "").strip():
                    href = ff.text.strip()
                    if self.base_loc:
                        href = self.base_loc.rstrip("/") + "/" + href
                    cand = self.doc.pkg.resolve(self.entry, href)
                    if self.doc.pkg.has(cand):
                        return cand
                    if self.doc.pkg.has(href):
                        return href
        return None


class FontSpec(object):
    __slots__ = ("id", "name", "family", "charset", "serif", "bold", "italic",
                 "embedded", "owner")

    def __init__(self, fid, name, family, charset, serif, bold, italic, embedded, owner):
        self.id = fid
        self.name = name
        self.family = family
        self.charset = charset
        self.serif = serif
        self.bold = bold
        self.italic = italic
        self.embedded = embedded
        self.owner = owner

    def display_name(self) -> str:
        return self.family or self.name or ("Font-%s" % self.id)

    def __repr__(self):
        return "FontSpec(%s, %s)" % (self.id, self.display_name())


def parse_draw_param(el) -> dict:
    """把一个 <DrawParam> 元素解析成属性字典。

    颜色是子元素：
        <ofd:DrawParam ID="8" LineWidth="0.25">
          <ofd:FillColor Value="128 0 0" ColorSpace="5"/>
          <ofd:StrokeColor Value="128 0 0" ColorSpace="5"/>
        </ofd:DrawParam>
    """
    info = {
        "LineWidth": None,
        "Join": el.get("Join"),
        "Cap": el.get("Cap"),
        "DashPattern": el.get("DashPattern"),
        "MiterLimit": None,
        "FillColor": None,
        "StrokeColor": None,
    }
    lw = el.get("LineWidth")
    if lw is not None and str(lw).strip() != "":
        info["LineWidth"] = X.fnum(lw, None)
    ml = el.get("MiterLimit")
    if ml is not None and str(ml).strip() != "":
        info["MiterLimit"] = X.fnum(ml, None)
    for tag, key in (("FillColor", "FillColor"), ("StrokeColor", "StrokeColor")):
        child = X.find(el, tag)
        if child is not None:
            info[key] = colorlib.parse_color(
                child.get("Value"), child.get("ColorSpace"), child.get("Alpha"), None)
    return info


class DocumentRes(object):
    """DocumentRes.xml：图片等多媒体资源，以及部分实现放这里的绘制参数。"""

    def __init__(self, doc: Document, entry: str):
        self.doc = doc
        self.entry = entry
        self.root = None
        self.base_loc = ""
        self.multimedia: Dict[str, str] = {}
        self.drawparams: Dict[str, dict] = {}

    def load(self) -> None:
        root = self.doc.pkg.parse_xml(self.entry)
        if root is None:
            return
        self.root = root
        self.base_loc = (root.get("BaseLoc") or "").strip()
        for mm in X.deepfind_all(root, "MultiMedia"):
            mid = mm.get("ID") or ""
            if not mid:
                continue
            mf = X.find(mm, "MediaFile")
            if mf is not None and (mf.text or "").strip():
                self.multimedia[mid] = mf.text.strip()
        for dp in X.deepfind_all(root, "DrawParam"):
            did = dp.get("ID") or ""
            if did:
                self.drawparams[did] = parse_draw_param(dp)

    def entry_of(self, media_id: str) -> Optional[str]:
        """把 MultiMedia ID 解成包内路径（BaseLoc + MediaFile）。"""
        name = self.multimedia.get(str(media_id))
        if not name:
            return None
        candidates = []
        if self.base_loc:
            candidates.append(self.doc.pkg.resolve(
                self.entry, self.base_loc.rstrip("/") + "/" + name))
        candidates.append(self.doc.pkg.resolve(self.entry, name))
        candidates.append(name.lstrip("/"))
        for c in candidates:
            if c and self.doc.pkg.has(c):
                return c
        return candidates[0] if candidates else None


# ==================================================================== 页面

class Page(object):
    """一页。Content.xml 惰性解析。

    页面可以把文档级模板（CommonData/TemplatePage）叠在自己身上：
        <ofd:Page><ofd:Template TemplateID="1"/><ofd:Area>…
    模板里的对象参与渲染与命中，但它们的 page 指向模板本身，
    所以编辑它们改的是模板文件，不会写到正文页的 Content.xml 里。
    """

    def __init__(self, doc: Document, element):
        self.doc = doc
        self.element = element
        self.id = element.get("ID") or ""
        self.base_loc = element.get("BaseLoc") or ""
        self.content_entry = doc.pkg.resolve(doc.entry, self.base_loc)
        self.content_root = None
        self.layers: List["Layer"] = []
        self.warnings: List[str] = []
        self.template_refs: List[Tuple[str, Optional[str]]] = []
        self.annotations: List["Annotation"] = []
        self._size_cache: Optional[geo.Rect] = None

    # ---------------------------------------------------------- 装载
    def load_contents(self) -> None:
        self.layers = []
        self.warnings = []
        # 优先复用已解析的树：编辑时元素是挂在这棵树上的，
        # 重新从字节解析会把刚 append 的对象丢掉。
        root = self.doc.pkg.xml_roots.get(self.content_entry)
        if root is None:
            data = self.doc.pkg.entries.get(self.content_entry)
            if data is None:
                self.content_root = None
                self.warnings.append("Content.xml 缺失: %s" % self.content_entry)
                return
            try:
                root = X.parse(data)
            except Exception as exc:
                # 不良构的 XML 不能静默当成空页：真实文件损坏时用户要能看见原因
                self.content_root = None
                self.warnings.append("Content.xml 解析失败（%s）: %s"
                                     % (self.content_entry, exc))
                return
            self.doc.pkg.xml_roots[self.content_entry] = root
        self.content_root = root
        # 本页引用的文档级模板。两种写法都要认：
        #   1) <ofd:Page><ofd:Template TemplateID="1"/>
        #   2) <ofd:PageBlock><ofd:Template TemplateID="50" ZOrder="Background"/>
        # 所以用深度查找，只取带 TemplateID 的。
        self.template_refs = []
        for t in X.deepfind_all(root, "Template"):
            tid = (t.get("TemplateID") or "").strip()
            if tid:
                self.template_refs.append((tid, t.get("ZOrder")))
        # Content 节点可以多个；Layer 也有可能直接挂在 Page 下
        containers = X.find_all(root, "Content")
        if containers:
            for c in containers:
                for lel in X.find_all(c, "Layer"):
                    self.layers.append(Layer(self, lel))
        for lel in X.find_all(root, "Layer"):
            self.layers.append(Layer(self, lel))
        if not self.layers and containers:
            # Content 里什么都没有：补一个空 Layer 便于插入对象
            self.add_layer()
        self.load_page_resources()

    def load_page_resources(self) -> None:
        """页级资源 PageRes（表12：页资源，指向该页使用的资源文件）。

        结构和 PublicRes 一样，加载后并入文档级资源索引，这样
        find_font / find_colorspace / find_drawparam 自动能查到本页私有资源。
        """
        if self.content_root is None:
            return
        el = X.find(self.content_root, "PageRes")
        if el is None:
            return
        href = (el.text or "").strip()
        if not href:
            return
        entry = self.doc.pkg.resolve(self.content_entry, href)
        if not self.doc.pkg.has(entry):
            self.warnings.append("PageRes 缺失: %s" % entry)
            return
        if entry in self.doc.page_res_loaded:
            return
        res = PublicRes(self.doc, entry)
        res.load()
        self.doc.public_res.append(res)
        self.doc.page_res_loaded.add(entry)

    def add_layer(self, name: str = "Body") -> "Layer":
        if self.content_root is None:
            self.content_root = X.new_element("Page")
        cont = X.find(self.content_root, "Content")
        if cont is None:
            cont = X.append_new(self.content_root, "Content")
        lel = X.append_new(cont, "Layer", ID=self.doc.pkg.next_id(), Type=name)
        layer = Layer(self, lel)
        self.layers.append(layer)
        self.doc.pkg.touch(self.content_entry)
        return layer

    # ---------------------------------------------------------- 属性
    def own_area(self) -> Optional[geo.Rect]:
        """本文件 Area/PhysicalBox 定义的页面区域，没有就返回 None。"""
        if self.content_root is None:
            return None
        box = X.find_path(self.content_root, "Area", "PhysicalBox")
        if box is not None and (box.text or "").strip():
            r = geo.Rect.parse(box.text)
            if not r.is_empty():
                return r
        return None

    def load_annotations(self) -> None:
        """加载本页的分页注释文件（15.2）。

        根元素 PageAnnot，下面若干 Annot。注释内容与版式内容分开描述，
        所以这里独立解析，不动 Content.xml。
        """
        self.annotations = []
        entry = self.doc.annotation_index.get(self.id)
        if not entry:
            return
        root = self.doc.pkg.parse_xml(entry)
        if root is None:
            self.warnings.append("注释文件解析失败: %s" % entry)
            return
        holders = X.find_all(root, "Annot")
        if not holders:
            holders = X.deepfind_all(root, "Annot")
        for ael in holders:
            self.annotations.append(Annotation(self, ael))

    def annotation_objects(self, printable_only: bool = False) -> List["OFDObject"]:
        """注释里挂的对象。装订/打印时传 printable_only=True 跳过 Print=false。"""
        out: List["OFDObject"] = []
        for anno in self.annotations:
            if not anno.visible:
                continue
            if printable_only and not anno.printable:
                continue
            out.extend(anno.objects())
        return out

    def size(self) -> geo.Rect:
        """页面物理尺寸（mm）。

        GB/T 33190 表12：页面 Area 不出现时，先用所引用模板页的定义，
        仍然没有才用 CommonData 里的 PageArea。
        """
        if self._size_cache is not None:
            return self._size_cache
        size = self.own_area()
        if size is None:
            for tid, _z in self.template_refs:
                tpl = self.doc.templates.get(str(tid))
                if tpl is not None:
                    size = tpl.own_area()
                    if size is not None:
                        break
        if size is None:
            size = self.doc.default_page_size()
        self._size_cache = size
        return size

    def set_size(self, rect: geo.Rect) -> None:
        if self.content_root is None:
            return
        area = X.ensure_path(self.content_root, "Area")
        box = X.find(area, "PhysicalBox")
        if box is None:
            box = X.append_new(area, "PhysicalBox")
        box.text = rect.to_str()
        self._size_cache = rect
        self.doc.pkg.touch(self.content_entry)

    def rotation(self) -> int:
        try:
            return int(X.fnum(self.element.get("Rotation"), 0)) % 360
        except ValueError:
            return 0

    def set_rotation(self, deg: int) -> None:
        self.element.set("Rotation", str(int(deg) % 360))
        self.doc.pkg.touch(self.doc.entry)

    def all_layers(self) -> List["Layer"]:
        """按绘制顺序的完整图层列表。

        背景模板 → 本页图层 → 前景模板 → 注释。
        第15章把注释定义为"版式文档形成后附加的图文信息"，所以叠在最上面；
        签章（Stamp）就在这一层，漏掉它页面就少个章。
        """
        bg: List["Layer"] = []
        fg: List["Layer"] = []
        for tid, zover in self.template_refs:
            tpl = self.doc.templates.get(str(tid))
            if tpl is None:
                continue
            z = (zover or tpl.zorder or "Background").lower()
            (fg if z.startswith("fore") else bg).extend(tpl.layers)
        layers = bg + list(self.layers) + fg
        for anno in self.annotations:
            if anno.visible and anno.layer is not None:
                layers.append(anno.layer)
        return layers

    def objects(self) -> List["OFDObject"]:
        """按绘制顺序拍平：背景模板 → 本页图层 → 前景模板。"""
        out: List["OFDObject"] = []
        for layer in self.all_layers():
            out.extend(layer.flat_objects())
        return out

    def hit_test(self, x: float, y: float, mode: str = "box", tol: float = 0.6) -> Optional["OFDObject"]:
        """页面坐标（mm）命中测试，返回最上层对象。"""
        for obj in reversed(self.objects()):
            if obj.hit_test(x, y, mode, tol):
                return obj
        return None

    def bbox_of_objects(self) -> geo.Rect:
        bb = geo.BBox()
        for obj in self.objects():
            r = obj.world_bounds()
            if r.is_empty():
                continue
            for px, py in r.corners():
                bb.add(px, py)
        return bb.rect()

    def add_object(self, obj: "OFDObject", layer_index: int = -1) -> None:
        if not self.layers:
            self.add_layer()
        layer = self.layers[layer_index] if self.layers else self.add_layer()
        layer.element.append(obj.element)
        self.doc.pkg.touch(self.content_entry)

    def remove_object(self, obj: "OFDObject") -> bool:
        for layer in self.layers:
            for parent, child in _walk(layer.element):
                if child is obj.element:
                    parent.remove(child)
                    self.doc.pkg.touch(self.content_entry)
                    return True
        return False

    def content_snapshot(self):
        """整个 Content.xml 的深拷贝，用于 undo。"""
        if self.content_root is None:
            return None
        return X.clone(self.content_root)

    def restore_snapshot(self, snap) -> None:
        if snap is None:
            return
        if self.content_root is not None:
            self.content_root.clear()
            self.content_root.tag = snap.tag
            for k, v in snap.attrib.items():
                self.content_root.set(k, v)
            for ch in list(snap):
                self.content_root.append(X.clone(ch))
            self.doc.pkg.touch(self.content_entry)
            self.load_contents()

    def __repr__(self):
        return "Page(%s, %d layers)" % (self.id, len(self.layers))


def geoC(fallback: geo.Rect, r: geo.Rect) -> geo.Rect:
    return r if not r.is_empty() else fallback


class TemplatePage(object):
    """文档级模板页（CommonData/TemplatePage）。

    它有和 Page 一样的结构（Area + Content/Layer），也能承载对象，
    但不属于 Pages 列表：页面通过 <Template TemplateID="…"/> 引用它。
    这里刻意让接口贴近 Page（doc / content_entry / size() / layers / objects()），
    这样 Layer 与对象的代码不用为模板写第二套。
    """

    def __init__(self, doc: "Document", element):
        self.doc = doc
        self.element = element
        self.id = (element.get("ID") or "").strip()
        self.zorder = (element.get("ZOrder") or "Background").strip()
        self.base_loc = (element.get("BaseLoc") or "").strip()
        self.content_entry = doc.pkg.resolve(doc.entry, self.base_loc)
        self.content_root = None
        self.layers: List["Layer"] = []
        self.warnings: List[str] = []
        self._size_cache: Optional[geo.Rect] = None
        self.load_contents()

    def load_contents(self) -> None:
        self.layers = []
        self.warnings = []
        root = self.doc.pkg.xml_roots.get(self.content_entry)
        if root is None:
            data = self.doc.pkg.entries.get(self.content_entry)
            if data is None:
                self.warnings.append("模板 Content.xml 缺失: %s" % self.content_entry)
                return
            try:
                root = X.parse(data)
            except Exception as exc:
                self.warnings.append("模板 Content.xml 解析失败（%s）: %s"
                                     % (self.content_entry, exc))
                return
            self.doc.pkg.xml_roots[self.content_entry] = root
        self.content_root = root
        for c in X.find_all(root, "Content"):
            for lel in X.find_all(c, "Layer"):
                self.layers.append(Layer(self, lel))
        for lel in X.find_all(root, "Layer"):
            self.layers.append(Layer(self, lel))

    def own_area(self) -> Optional[geo.Rect]:
        if self.content_root is None:
            return None
        box = X.find_path(self.content_root, "Area", "PhysicalBox")
        if box is not None and (box.text or "").strip():
            r = geo.Rect.parse(box.text)
            if not r.is_empty():
                return r
        return None

    def size(self) -> geo.Rect:
        if self._size_cache is not None:
            return self._size_cache
        self._size_cache = self.own_area() or self.doc.default_page_size()
        return self._size_cache

    def rotation(self) -> int:
        return 0

    def objects(self) -> List["OFDObject"]:
        out: List["OFDObject"] = []
        for layer in self.layers:
            out.extend(layer.flat_objects())
        return out

    def all_layers(self) -> List["Layer"]:
        """模板页自身没有嵌套模板，直接返回自己的图层。"""
        return list(self.layers)

    # 模板对象也能编辑（改票头文字、水印），所以快照接口要和 Page 一致
    def content_snapshot(self):
        if self.content_root is None:
            return None
        return X.clone(self.content_root)

    def restore_snapshot(self, snap) -> None:
        if snap is None or self.content_root is None:
            return
        self.content_root.clear()
        self.content_root.tag = snap.tag
        for k, v in snap.attrib.items():
            self.content_root.set(k, v)
        for ch in list(snap):
            self.content_root.append(X.clone(ch))
        self.doc.pkg.touch(self.content_entry)
        self.load_contents()

    def describe(self) -> str:
        return "模板页 #%s（%s）%d 个对象" % (self.id, self.zorder, len(self.objects()))

    def __repr__(self):
        return "TemplatePage(%s, %s, %d layers)" % (
            self.id, self.zorder, len(self.layers))


class Annotation(object):
    """一条注释（GB/T 33190 第15章）。

    Type 取值见表62：Link / Path / Highlight / Stamp / Watermark。
    外观在 Appearance（CT_PageBlock），结构与页面内容块相同，
    所以直接复用一个 Layer 承载它 —— 签章图片就躺在这里面（见 18.2.3
    StampAnnot "本签名关联的外观，用 OFD 中的注释来表示"）。
    """

    __slots__ = ("page", "element", "id", "type", "subtype", "creator",
                 "last_mod", "visible", "printable", "no_zoom", "no_rotate",
                 "readonly", "remark", "parameters", "layer")

    def __init__(self, page: "Page", element):
        self.page = page
        self.element = element
        self.id = (element.get("ID") or "").strip()
        self.type = (element.get("Type") or "").strip()
        self.subtype = element.get("Subtype") or ""
        self.creator = element.get("Creator") or ""
        self.last_mod = element.get("LastModDate") or ""
        # 表61 的默认值：Visible/Print/ReadOnly 默认 true，NoZoom/NoRotate 默认 false
        self.visible = X.bool_attr(element.get("Visible"), True)
        self.printable = X.bool_attr(element.get("Print"), True)
        self.no_zoom = X.bool_attr(element.get("NoZoom"), False)
        self.no_rotate = X.bool_attr(element.get("NoRotate"), False)
        self.readonly = X.bool_attr(element.get("ReadOnly"), True)
        self.remark = element.get("Remark") or ""
        self.parameters: Dict[str, str] = {}
        for p in X.deepfind_all(element, "Parameter"):
            name = p.get("Name")
            if name:
                self.parameters[name] = (p.text or "").strip()
        appearance = X.find(element, "Appearance")
        self.layer = Layer(page, appearance) if appearance is not None else None

    def objects(self) -> List["OFDObject"]:
        return self.layer.flat_objects() if self.layer is not None else []

    def is_stamp(self) -> bool:
        return self.type.lower() == "stamp"

    def describe(self) -> str:
        return "注释 #%s %s（%d 个对象）" % (
            self.id, self.type or "?", len(self.objects()))

    def __repr__(self):
        return "Annotation(%s, %s, %d objs)" % (
            self.id, self.type, len(self.objects()))


def _walk(el):
    """产出 (parent, child) 对。"""
    for ch in list(el):
        yield el, ch
        for pair in _walk(ch):
            yield pair


class Layer(object):
    """图层。"""

    def __init__(self, page: Page, element):
        self.page = page
        self.element = element
        self.id = element.get("ID") or ""
        self.type = element.get("Type") or "Body"
        self._objects: List["OFDObject"] = []
        self._build()

    def _build(self) -> None:
        self._objects = []
        for ch in list(self.element):
            cls = _OBJ_TYPES.get(X.local(ch.tag))
            if cls is not None:
                self._objects.append(cls(self.page, ch, self))

    def rebuild(self) -> None:
        self._build()

    def flat_objects(self) -> List["OFDObject"]:
        out = []
        for o in self._objects:
            out.append(o)
            out.extend(o.children())
        return out

    def top_objects(self) -> List["OFDObject"]:
        return list(self._objects)


# ==================================================================== 对象基类

class OFDObject(object):
    """所有页面对象的基类。包装一个 XML 元素。"""

    TYPE = "Object"
    # 这两个默认值按对象类型不同，别用一套：
    #   表35 图形对象：Stroke 默认 true、Fill 默认 false、FillColor 默认透明
    #   表45 文字对象：Stroke 默认 false、Fill 默认 true、FillColor 默认黑色
    FILL_DEFAULT = True
    STROKE_DEFAULT = False
    FILL_COLOR_DEFAULT = (0.0, 0.0, 0.0, 1.0)

    def __init__(self, page: Page, element, layer: Optional[Layer] = None):
        self.page = page
        self.element = element
        self.layer = layer
        self.id = element.get("ID") or ""

    # ---------------------------------------------------------- 基本属性
    @property
    def type_name(self) -> str:
        return self.TYPE

    def boundary(self) -> geo.Rect:
        return geo.Rect.parse(self.element.get("Boundary"))

    def set_boundary(self, rect: geo.Rect) -> None:
        self.element.set("Boundary", rect.to_str())
        self.page.doc.pkg.touch(self.page.content_entry)

    def ctm(self) -> geo.Matrix:
        return geo.Matrix.parse(self.element.get("CTM"))

    def set_ctm(self, m: geo.Matrix) -> None:
        if m.is_identity():
            if "CTM" in self.element.attrib:
                del self.element.attrib["CTM"]
        else:
            self.element.set("CTM", m.to_str())
        self.page.doc.pkg.touch(self.page.content_entry)

    def world_matrix(self) -> geo.Matrix:
        """对象坐标 → 页面坐标。"""
        return self.ctm()

    def world_bounds(self) -> geo.Rect:
        """对象包围盒映射到页面坐标后的轴对齐矩形。"""
        r = self.boundary().normalized()
        if r.is_empty():
            return r
        m = self.world_matrix()
        bb = geo.BBox()
        for x, y in r.corners():
            bb.add(*m.apply(x, y))
        return bb.rect()

    def alpha(self) -> float:
        return min(1.0, max(0.0, X.fnum(self.element.get("Alpha"), 255) / 255.0))

    # ---------------------------------------------------------- 绘制参数
    def draw_param(self) -> Optional[dict]:
        """取 DrawParam。对象上没有就往上找 Layer，再没有就是没有。"""
        ref = self.element.get("DrawParam")
        if not ref and self.layer is not None:
            ref = self.layer.element.get("DrawParam")
        if not ref:
            return None
        return self.page.doc.find_drawparam(ref)

    def _dp(self, key, default=None):
        dp = self.draw_param()
        if dp is None:
            return default
        val = dp.get(key)
        return default if val is None else val

    # ---------------------------------------------------------- 颜色
    def _color_attr(self, attr: str, cs_attr: str, default=None):
        raw = self.element.get(attr)
        cs = self.element.get(cs_attr) or self.element.get("ColorSpace")
        return colorlib.parse_color(raw, cs, self.element.get("Alpha"), default)

    def _color_child(self, child_name: str):
        """颜色也可能写成子元素：<ofd:FillColor Value="128 0 0" ColorSpace="5"/>

        数电发票、部分公文生成器都用这种写法。只读属性会漏掉全部颜色。
        """
        el = X.find(self.element, child_name)
        if el is None:
            return None
        value = el.get("Value")
        if value is None:
            value = el.get("Color") or (el.text or "")
        return colorlib.parse_color(value, el.get("ColorSpace"),
                                    el.get("Alpha"), None)

    def has_fill(self) -> bool:
        """是否填充。默认值按对象类型区分（表35 / 表45）。"""
        raw = self.element.get("Fill")
        if raw is not None and str(raw).strip() != "":
            return X.bool_attr(raw, self.FILL_DEFAULT)
        return self.FILL_DEFAULT

    def has_stroke(self) -> bool:
        """是否勾边。图形对象默认 true，文字对象默认 false。"""
        raw = self.element.get("Stroke")
        if raw is not None and str(raw).strip() != "":
            return X.bool_attr(raw, self.STROKE_DEFAULT)
        if X.find(self.element, "StrokeColor") is not None:
            return True
        if X.find(self.element, "StrokePattern") is not None:
            return True
        if self.element.get("LineWidth") is not None:
            return True
        return self.STROKE_DEFAULT

    def fill_color(self):
        c = self._color_child("FillColor")
        if c is not None:
            return c
        c = self._dp("FillColor")
        if c is not None:
            return c
        # 表35：图形对象的 FillColor 默认是透明色（不是黑）
        return self._color_attr("FillColor", "FillColorSpace", self.FILL_COLOR_DEFAULT)

    def stroke_color(self):
        c = self._color_child("StrokeColor")
        if c is not None:
            return c
        c = self._dp("StrokeColor")
        if c is not None:
            return c
        return self._color_attr("StrokeColor", "StrokeColorSpace", (0, 0, 0, 1))

    def line_width(self) -> float:
        w = self.element.get("LineWidth")
        if w is not None and str(w).strip() != "":
            return X.fnum(w, 0.35)
        dp = self._dp("LineWidth")
        return float(dp) if dp is not None else 0.35

    def line_join(self) -> str:
        return (self.element.get("Join") or self._dp("Join") or "round").lower()

    def line_cap(self) -> str:
        return (self.element.get("Cap") or self._dp("Cap") or "round").lower()

    def dash_pattern(self):
        dp_raw = self.element.get("DashPattern")
        if not dp_raw:
            dp_val = self._dp("DashPattern")
            dp_raw = dp_val if isinstance(dp_val, str) else None
        if not dp_raw or dp_raw == "0":
            return None
        vals = X.fnum_list(dp_raw)
        if not vals or all(v <= 0 for v in vals):
            return None
        return vals

    # ---------------------------------------------------------- 命中
    def hit_test(self, x: float, y: float, mode: str = "box", tol: float = 0.6) -> bool:
        r = self.boundary().normalized()
        if r.is_empty():
            return False
        inv = self.world_matrix().invert()
        lx, ly = inv.apply(x, y)
        if mode == "box":
            return r.contains(lx, ly, tol)
        return self._hit_local(lx, ly, tol)

    def _hit_local(self, lx: float, ly: float, tol: float) -> bool:
        return self.boundary().normalized().contains(lx, ly, tol)

    # ---------------------------------------------------------- 子对象
    def children(self) -> List["OFDObject"]:
        return []

    def describe(self) -> str:
        return "%s #%s" % (self.TYPE, self.id)

    def __repr__(self):
        return "<%s #%s>" % (self.TYPE, self.id)


@register_object("TextObject")
class TextObject(OFDObject):
    """文本对象。

    TextCode 可以有多个，每个带 X/Y（相对 Boundary 对象坐标，单位 mm，
    Y 是基线位置）。DeltaX 是相邻字形绘制点的距离，单位 mm。
    TextCode 的 X/Y 省略时沿用上一个 TextCode 的值（表46）。

    默认值按表45：Fill=true、Stroke=false、FillColor=黑。
    """

    TYPE = "文本"
    FILL_DEFAULT = True
    STROKE_DEFAULT = False
    FILL_COLOR_DEFAULT = (0.0, 0.0, 0.0, 1.0)

    # ---------------------------------------------------------- 字体
    def font_id(self) -> str:
        ref = self.element.get("Font") or ""
        # Font 可能是 "3" 或 "3 4"（多字体回退）
        return ref.split()[0] if ref.strip() else ""

    def font(self):
        fid = self.font_id()
        if not fid:
            return None
        return self.page.doc.find_font(fid)

    def size_mm(self) -> float:
        """字号，单位 mm。有些文件把 Size 写成 pt，按 <1 判断不现实，统一按 mm。"""
        return X.fnum(self.element.get("Size"), 3.0)

    def set_size_mm(self, v: float) -> None:
        self.element.set("Size", geo._fmt(v))
        self.page.doc.pkg.touch(self.page.content_entry)

    def bold(self) -> bool:
        return X.bool_attr(self.element.get("Weight"), False) or int(X.fnum(self.element.get("Weight"), 0)) >= 600

    def italic(self) -> bool:
        return X.bool_attr(self.element.get("Italic"), False)

    def char_spacing(self) -> float:
        return X.fnum(self.element.get("CharSpacing"), 0.0)

    # ---------------------------------------------------------- 文本内容
    def text_codes(self) -> List["TextCode"]:
        return [TextCode(el) for el in X.find_all(self.element, "TextCode")]

    def text(self) -> str:
        parts = []
        for tc in self.text_codes():
            parts.append(tc.text)
        return "".join(parts)

    def set_text(self, text: str) -> None:
        """改写文本。

        保留第一个 TextCode 的定位，把整段文本塞进去；多余的 TextCode 删除。
        这是最稳的做法：不重建坐标系，不破坏排版基准点。
        """
        codes = X.find_all(self.element, "TextCode")
        if not codes:
            tc = X.append_new(self.element, "TextCode")
            tc.set("X", "0")
            tc.set("Y", "0")
            codes = [tc]
        first = codes[0]
        first.set("Text", text)
        for extra in codes[1:]:
            self.element.remove(extra)
        # 单行文本的长度跟着内容走，重算 Boundary 宽度
        r = self.boundary()
        if r.w > 0:
            est = self.estimate_text_width(text)
            if est > 0:
                r.w = max(est, self.size_mm() * 0.5)
                self.set_boundary(r)
        self.page.doc.pkg.touch(self.page.content_entry)

    def estimate_text_width(self, text: str) -> float:
        """粗估文本宽度（mm）。CJK 按 1em，ASCII 按 0.5em。"""
        em = self.size_mm()
        total = 0.0
        for ch in text:
            if ord(ch) > 0x2E80:      # CJK 及全角
                total += em * 1.0
            elif ch in "ilI.,:;'|!":
                total += em * 0.28
            elif ch == " ":
                total += em * 0.25
            else:
                total += em * 0.52
        total += self.char_spacing() * max(0, len(text) - 1)
        return total

    def _hit_local(self, lx: float, ly: float, tol: float) -> bool:
        r = self.boundary().normalized()
        return r.contains(lx, ly, tol)

    def describe(self) -> str:
        t = self.text()
        if len(t) > 24:
            t = t[:24] + "…"
        return "文本 #%s  %s" % (self.id, t)


class TextCode(object):
    __slots__ = ("element",)

    def __init__(self, element):
        self.element = element

    @property
    def x(self) -> float:
        return X.fnum(self.element.get("X"), 0)

    @property
    def y(self) -> float:
        return X.fnum(self.element.get("Y"), 0)

    @property
    def text(self) -> str:
        """文本有两种写法：Text 属性，或元素内容。

        标准两种都允许。数电发票一类生成器（GUOPIAOOFDSDK）用的是
        <ofd:TextCode ...>开票人姓名</ofd:TextCode>，只读 Text 属性会得到空串。
        """
        t = self.element.get("Text")
        if t is not None:
            return t
        return (self.element.text or "")

    @property
    def delta_x(self) -> List[float]:
        return X.fnum_list(self.element.get("DeltaX"))

    @property
    def delta_y(self) -> List[float]:
        return X.fnum_list(self.element.get("DeltaY"))

    def __repr__(self):
        return "TextCode(%s)" % self.text


@register_object("PathObject")
class PathObject(OFDObject):
    """路径对象：线条、矩形、任意曲线。

    路径数据有两种承载方式（标准 schema 只认第 2 种，第 1 种是为了兼容
    用了 <Path Data="…"/> 的导出器；写出时会被 normalize 转成第 2 种）：
      1) <ofd:Path Data="M 0 0 L 10 0"/>
      2) <ofd:AbbreviatedData>M 0 0 L 10 0</ofd:AbbreviatedData>

    默认值按表35：Stroke=true、Fill=false、FillColor=透明、StrokeColor=黑。
    """

    TYPE = "路径"
    FILL_DEFAULT = False
    STROKE_DEFAULT = True
    FILL_COLOR_DEFAULT = (0.0, 0.0, 0.0, 0.0)   # 透明

    def path_data(self) -> str:
        p = X.find(self.element, "Path")
        if p is not None:
            d = p.get("Data")
            if d:
                return d
            if (p.text or "").strip():
                return p.text.strip()
        ab = X.find(self.element, "AbbreviatedData")
        if ab is not None:
            return (ab.text or "").strip()
        return ""

    def set_path_data(self, data: str) -> None:
        p = X.find(self.element, "Path")
        if p is None:
            ab = X.find(self.element, "AbbreviatedData")
            if ab is not None:
                ab.text = data
                self.page.doc.pkg.touch(self.page.content_entry)
                return
            p = X.append_new(self.element, "Path")
        p.set("Data", data)
        self.page.doc.pkg.touch(self.page.content_entry)

    def subpaths(self, with_ctm: bool = False) -> List[geo.SubPath]:
        data = self.path_data()
        m = self.world_matrix() if with_ctm else None
        return geo.parse_path_data(data, m)

    def is_closed_fillable(self) -> bool:
        return self.has_fill()

    def _hit_local(self, lx: float, ly: float, tol: float) -> bool:
        b = self.boundary()
        subs = self.subpaths(False)
        # 路径点以 Boundary 左上角为原点
        px, py = lx - b.x, ly - b.y
        if not subs:
            return b.normalized().contains(lx, ly, tol)
        for sp in subs:
            if sp.closed and self.has_fill() and geo.point_in_polygon(px, py, sp.points):
                return True
            if geo.point_near_polyline(px, py, sp.points,
                                       max(tol, self.line_width() / 2 + tol)):
                return True
        return False

    def describe(self) -> str:
        n = len(self.subpaths())
        return "路径 #%s  %d 条子路径" % (self.id, n)


@register_object("ImageObject")
class ImageObject(OFDObject):
    """图片对象。"""

    TYPE = "图片"

    def resource_id(self) -> str:
        return self.element.get("ResourceID") or ""

    def image_bytes(self) -> Optional[Tuple[str, bytes]]:
        return self.page.doc.find_image(self.resource_id())

    def _hit_local(self, lx: float, ly: float, tol: float) -> bool:
        return self.boundary().normalized().contains(lx, ly, tol)

    def describe(self) -> str:
        got = self.image_bytes()
        return "图片 #%s  %s" % (self.id, got[0] if got else "资源缺失")


@register_object("CompositeObject")
class CompositeObject(OFDObject):
    """组合对象：内部再挂若干对象，可能有自己的 CTM。"""

    TYPE = "组合"

    def children(self) -> List["OFDObject"]:
        out = []
        for ch in list(self.element):
            cls = _OBJ_TYPES.get(X.local(ch.tag))
            if cls is not None:
                out.append(cls(self.page, ch, self.layer))
        flat = []
        for o in out:
            flat.append(o)
            flat.extend(o.children())
        return flat

    def _hit_local(self, lx: float, ly: float, tol: float) -> bool:
        for ch in self.children():
            if ch.hit_test(lx, ly, "box", tol):
                # 子对象在自己的坐标系里，这里只做包围盒粗判
                return True
        return self.boundary().normalized().contains(lx, ly, tol)


# 其余类型统一兜底
class GenericObject(OFDObject):
    TYPE = "对象"


def wrap_object(page: Page, element, layer: Optional[Layer] = None) -> OFDObject:
    cls = _OBJ_TYPES.get(X.local(element.tag), GenericObject)
    return cls(page, element, layer)

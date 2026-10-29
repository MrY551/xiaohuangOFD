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
from . import extras
from . import geometry as geo
from . import xmlutil as X
from .errors import ParseError, ResourceMissing

extras.bind(X)

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

def _abbrev_to_subpaths(data: str):
    """把 AbbreviatedData 解析成若干子路径的点序列（mm，相对对象空间原点）。

    裁剪只需要折线点，曲线按固定段数压平——裁剪边界宁可略保守也不要错位。
    真正的解析在 geometry.parse_path_data（它已经把曲线压平成折线）。
    """
    try:
        from . import geometry as geo
        subs = geo.parse_path_data(data)
    except Exception:
        return []
    out = []
    for sp in subs:
        pts = []
        # SubPath 可能是 (points, closed) 或带 .points 的对象，两种都兼容
        raw = sp.points if hasattr(sp, "points") else (sp[0] if isinstance(sp, tuple) else sp)
        for pt in raw:
            if isinstance(pt, (tuple, list)) and len(pt) >= 2:
                pts.extend([float(pt[0]), float(pt[1])])
        if len(pts) >= 4:
            out.append(pts)
    return out


class Shading(object):
    """渐变/底纹填充（8.3.3 / 8.3.4）。

    CT_Color 里除基本颜色外还能装五种填充，统一用这个类承载：

        Pattern          底纹（8.3.3）
        AxialShd         轴向渐变（线性）
        RadialShd        径向渐变（可带离心率与倾角）
        GouraudShd       高洛德渐变（三角网格顶点色插值）
        LaGourandShd     网格高洛德（二维网格插值，标准拼写如此，少个 u）

    坐标都是**对象空间**（相对 Boundary 左上角），与路径数据同一坐标系。
    """

    __slots__ = ("kind", "el", "segments", "attrs", "doc")

    KINDS = ("Pattern", "AxialShd", "RadialShd", "GouraudShd", "LaGourandShd")

    def __init__(self, kind: str, el, doc=None):
        self.kind = kind
        self.el = el
        self.attrs = el
        self.doc = doc
        self.segments = self._parse_segments(el)

    def _parse_segments(self, el):
        # 段颜色也是 CT_Color，ColorSpace 同样要回资源里查（8.3.1）
        return self._parse_segments_impl(el, self.doc)

    def _color(self, el, default=None):
        return parse_color_el(el, self.doc, default)

    @staticmethod
    def _parse_segments_impl(el, doc):
        """Segment：Position（0..1，可省）+ Color（CT_Color，基本颜色）。

        表29 / 表30：不写 Position 时按出现顺序平均分配；
        第一个默认 0，最后一个默认 1.0，中间的在空缺区间内均分。
        """
        segs = []
        for seg in X.find_all(el, "Segment"):
            color_el = X.find(seg, "Color")
            if color_el is None:
                continue
            rgba = parse_color_el(color_el, doc, (0, 0, 0, 1)) or (0, 0, 0, 1)
            pos = seg.get("Position")
            segs.append((None if pos in (None, "") else X.fnum(pos, None), rgba))
        if not segs:
            return []
        # 补 Position：首 0、尾 1.0、中间按已给的锚点分段均分
        n = len(segs)
        out = []
        for i, (pos, rgba) in enumerate(segs):
            if pos is None:
                if i == 0:
                    pos = 0.0
                elif i == n - 1:
                    pos = 1.0
            out.append([pos, rgba])
        # 中间空缺：在前后已知锚点之间均分
        known = [i for i, (pos, _) in enumerate(out) if pos is not None]
        for a, b in zip(known, known[1:]):
            if b - a > 1:
                p0, p1 = out[a][0], out[b][0]
                for k in range(a + 1, b):
                    out[k][0] = p0 + (p1 - p0) * (k - a) / float(b - a)
        for item in out:
            if item[0] is None:
                item[0] = 0.0
        out.sort(key=lambda x: x[0])
        return [(float(p), c) for p, c in out]

    # ---- 通用属性
    def map_type(self) -> str:
        return (self.el.get("MapType") or "Direct")

    def map_unit(self):
        v = self.el.get("MapUnit")
        return X.fnum(v, 0.0) if v not in (None, "") else None

    def extend(self) -> int:
        v = self.el.get("Extend")
        return int(X.fnum(v, 0)) if v not in (None, "") else 0

    def points(self):
        """返回 (StartPoint, EndPoint)，ST_Pos 两个值。"""
        def pair(name):
            v = X.fnum_list(self.el.get(name) or "")
            return (v[0], v[1]) if len(v) >= 2 else (0.0, 0.0)
        return pair("StartPoint"), pair("EndPoint")

    # ---- 径向专有
    def edge_flag(self):
        """表 31：三角单元切换的方向标志。

        决定网格单元里两条对角线用哪一条：0 走「左上-右下」，
        1 走「右上-左下」。网格高洛德（表 32）固定为 1。
        """
        v = self.el.get("EdgeFlag") if hasattr(self, "el") else None
        if v is None:
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    def radii(self):
        r0 = X.fnum(self.el.get("StartRadius"), 0.0)
        r1 = X.fnum(self.el.get("EndRadius"), 0.0)
        return r0, r1

    def eccentricity(self) -> float:
        return X.fnum(self.el.get("Eccentricity"), 0.0)

    def angle(self) -> float:
        return X.fnum(self.el.get("Angle"), 0.0)

    def control_points(self):
        """高洛德渐变的控制点：[{x, y, edge_flag, rgba}]，坐标是对象空间 mm。

        表31：Point 至少 3 个，X/Y 必选，Color 必选，EdgeFlag 可选。
        网格高洛德（LaGourandShd）只是把同一批点按 VerticesPerRow 组成网格，
        点的结构一样。
        """
        out = []
        for pt in X.find_all(self.el, "Point"):
            rgba = (0, 0, 0, 1)
            cel = X.find(pt, "Color")
            if cel is not None:
                rgba = self._color(cel, (0, 0, 0, 1)) or (0, 0, 0, 1)
            out.append({
                "x": X.fnum(pt.get("X"), 0.0),
                "y": X.fnum(pt.get("Y"), 0.0),
                "edge_flag": int(X.fnum(pt.get("EdgeFlag"), 0)),
                "rgba": rgba,
            })
        return out

    def back_color(self):
        cel = X.find(self.el, "BackColor")
        if cel is None:
            return None
        return self._color(cel, None)

    def vertices_per_row(self):
        v = self.el.get("VerticesPerRow")
        return int(X.fnum(v, 0)) if v not in (None, "") else None

    # ---------------------------------------------------------- 底纹（8.3.3）

    def pattern_attrs(self):
        """底纹的平铺参数（表 CT_Pattern）。

        Width / Height 是**单元格尺寸**（必选）；XStep / YStep 是平铺步长，
        不写时等于 Width / Height。RelativeTo 决定起点相对页面还是相对对象，
        默认 Object。ReflectMethod 控制相邻格是否镜像。
        """
        w = X.fnum(self.el.get("Width"), 0.0)
        h = X.fnum(self.el.get("Height"), 0.0)
        xs = self.el.get("XStep")
        ys = self.el.get("YStep")
        return {
            "w": w,
            "h": h,
            "xstep": X.fnum(xs, w) if xs not in (None, "") else w,
            "ystep": X.fnum(ys, h) if ys not in (None, "") else h,
            "reflect": (self.el.get("ReflectMethod") or "Normal"),
            "relative_to": (self.el.get("RelativeTo") or "Object"),
            "ctm": X.fnum_list(self.el.get("CTM")) or [],
        }

    def pattern_cell(self, page):
        """把 CellContent 里的图元包成对象列表（可递归含任意图元）。

        CellContent 是 CT_PageBlock，与页面图层同构，所以复用同一套对象工厂。
        没有 page 上下文时返回空（底纹必须挂在某个页上才能解析资源）。
        """
        node = X.find(self.el, "CellContent")
        if node is None or page is None:
            return []
        out = []
        for ch in list(node):
            cls = _OBJ_TYPES.get(X.local(ch.tag))
            if cls is not None:
                out.append(cls(page, ch, None))
        return out

    def describe(self) -> str:
        if self.kind in ("GouraudShd", "LaGourandShd"):
            return "%s %d 点" % (self.kind, len(self.control_points()))
        st, en = self.points()
        return "%s %d 段" % (self.kind, len(self.segments))


class OFDPackage(object):
    """一个 .ofd 文件（其实是 zip 容器）。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.entries: Dict[str, bytes] = {}       # zip 条目原始字节
        self.dirty: set = set()                    # 需要重写的条目名
        self.xml_roots: Dict[str, "object"] = {}   # 条目名 → Element（已解析）
        self.ofd_root = None                       # OFD.xml 的根
        self.doc_type = ""                         # 表3：OFD / OFD-A
        self.ofd_version = ""                      # 表3：固定 1.0
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
        # 表3：DocType 取值 OFD（符合本标准）或 OFD-A（符合存档规范），Version 固定 1.0
        self.doc_type = (self.ofd_root.get("DocType") or "").strip()
        self.ofd_version = (self.ofd_root.get("Version") or "").strip()

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
            # 7.5 表8/9/10 与 16/17 章在 Document 里；18/19 章挂在 OFD.xml 的 DocBody 上
            try:
                doc.load_extras(body, root_name)
            except Exception:
                # 附属结构坏了不该让整个文件打不开
                pass
            doc.doc_type = self.doc_type
            doc.ofd_version = self.ofd_version
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
        # ---- 第 7.5 / 16 / 17 / 18 / 19 / 20 章的文档级结构
        # 这些不参与排版，但决定"能不能导出/打印/看书签/取附件"。
        self.permissions: dict = dict(extras.PERM_DEFAULTS)
        self.vpreferences: dict = dict(extras.V_PREF_DEFAULTS)
        self.bookmarks: List[dict] = []
        self.attachments: List[dict] = []
        self.custom_tags: List[dict] = []
        self.extensions: List[dict] = []
        self.signatures: dict = {"items": []}
        self.versions: dict = {"items": []}
        self.doc_type = ""
        self.ofd_version = ""
        self.attachments_entry = ""
        self.custom_tags_entry = ""
        self.extensions_entry = ""

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

    def load_extras(self, body=None, ofd_entry: str = "") -> None:
        """文档级附属结构（7.5 表8/9/10、16、17、18、19、20 章）。

        这些章节不影响排版，但决定"能不能导出/打印、有没有书签/附件/签名"。
        解析失败一律退化成默认值，不给打开文件添堵。
        """
        # 表8 / 表9 / 表10：Document 根的直接子元素（Outlines 之后）
        self.permissions = extras.parse_permissions(self.root)
        self.vpreferences = extras.parse_vpreferences(self.root)
        self.bookmarks = extras.parse_bookmarks(self.root)

        # 表5：Attachments / CustomTags / Extensions 都是 ST_Loc
        parents = [p for p in (X.find(self.root, "CommonData"), self.root)
                   if p is not None]
        for attr, tag in (("attachments_entry", "Attachments"),
                          ("custom_tags_entry", "CustomTags"),
                          ("extensions_entry", "Extensions")):
            for parent in parents:
                el = X.find(parent, tag)
                if el is not None and (el.text or "").strip():
                    setattr(self, attr, self.pkg.resolve(
                        self.entry, el.text.strip()))
                    break
        self.attachments = extras.parse_attachments(self.pkg, self.attachments_entry)
        self.custom_tags = extras.parse_custom_tags(self.pkg, self.custom_tags_entry)
        self.extensions = extras.parse_extensions(self.pkg, self.extensions_entry)

        # 表3：Signatures / Versions 挂在 OFD.xml 的 DocBody 上（不是 Document）
        if body is not None:
            base = ofd_entry or self.entry
            el = X.find(body, "Signatures")
            if el is not None and (el.text or "").strip():
                self.signatures = extras.parse_signatures(
                    self.pkg, self.pkg.resolve(base, el.text.strip()))
            el = X.find(body, "Versions")
            if el is not None:
                if (el.text or "").strip():
                    # 版本列表指向独立文件
                    self.versions = extras.parse_versions(
                        self.pkg, self.pkg.resolve(base, el.text.strip()))
                else:
                    # 附录 A：版本列表直接内嵌在 OFD.xml 的 DocBody 里
                    self.versions = extras.parse_versions_el(el, self.pkg, base)

    def load_annotation_index(self) -> None:
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

    def find_vectorg(self, vid: str):
        """第 13 章：按 ID 取矢量图像资源（CT_VectorG）。"""
        key = str(vid)
        for res in self.public_res:
            got = res.vectorgs.get(key)
            if got is not None:
                return got
        if self.doc_res is not None and hasattr(self.doc_res, "vectorgs"):
            return self.doc_res.vectorgs.get(key)
        return None

    def find_colorspace(self, cs_id: str):
        """按 ID 取颜色空间描述：{type, bpc, palette}。

        8.3.1：ColorSpace 属性是 ST_RefID，要回资源文件里查 Type/BPC/调色板，
        不能拿 "1"/"5" 这种数字自己猜颜色空间。
        """
        if cs_id in (None, ""):
            return None
        key = str(cs_id)
        for res in self.public_res:
            cs = res.colorspaces.get(key)
            if cs:
                return cs
        return None

    def find_drawparam(self, dp_id: str, _depth: int = 0):
        """查 DrawParam。PublicRes 与 DocumentRes 两处都看。

        表21：绘制参数可以通过 Relative 继承**基础绘制参数**，形成嵌套；
        被继承的参数里的属性会传下来，子参数重定义的同名属性覆盖父参数。
        链上出现环（非法文件）时停手，不死循环。
        """
        key = str(dp_id)
        base = None
        for res in self.public_res:
            if key in res.drawparams:
                base = res.drawparams[key]
                break
        if base is None and self.doc_res is not None:
            base = self.doc_res.drawparams.get(key)
        if base is None:
            return None
        parent_id = base.get("Relative")
        if not parent_id or _depth > 16:
            return base
        parent = self.find_drawparam(parent_id, _depth + 1)
        if parent is None:
            return base
        merged = dict(parent)
        for k, v in base.items():
            if v is not None:
                merged[k] = v
        return merged

    def find_image(self, res_id: str) -> Optional[Tuple[str, bytes]]:
        """返回 (文件名, 字节)。找不到返回 None。"""
        if self.doc_res is None:
            return None
        entry = self.doc_res.entry_of(str(res_id))
        if not entry or not self.pkg.has(entry):
            return None
        return (os.path.basename(entry), self.pkg.read(entry))

    # ---------------------------------------------------------- 页面操作
    # ---------------------------------------------------------- 大纲（7.8）

    def outlines(self):
        """文档大纲树（7.8 / CT_OutlineElem）。

        返回 [{title, count, expanded, actions, children}] 的列表。
        标准里 Outlines 挂在 CommonData 下，每个 OutlineElem 可递归嵌套，
        Title 必选、Expanded 默认 true。
        """
        # 7.8：Outlines 是 Document 根的直接子元素，位置在 CommonData **之后**、
        # Permissions 之前（附录 A 的 Document sequence）。不是 CommonData 的子元素。
        node = X.find(self.root, "Outlines")
        if node is None:
            return []
        return [self._outline_elem(el) for el in X.find_all(node, "OutlineElem")]

    def _outline_elem(self, el):
        acts = []
        an = X.find(el, "Actions")
        if an is not None:
            acts = [a for a in X.find_all(an, "Action")]
        return {
            "title": el.get("Title") or "",
            "count": el.get("Count"),
            "expanded": X.bool_attr(el.get("Expanded"), True),
            "actions": [self._action_desc(a) for a in acts],
            "children": [self._outline_elem(c) for c in X.find_all(el, "OutlineElem")],
        }

    def _action_desc(self, el):
        """把一个 <Action> 解成可读描述（第14章 / CT_Action，表51-59）。

        五类动作全解析：Goto（表53/54）、URI（表56）、GotoA（表55）、
        Sound（表57）、Movie（表58）。执行仍然只做 goto / uri / 打开附件，
        音视频只报信息（本工具不放音视频）。
        """
        ev = (el.get("Event") or "CLICK").upper()
        out = {"event": ev}

        # 表51：Region 是动作的启动区域（9.3 非紧缩描述的那套写法）
        region = X.find(el, "Region")
        if region is not None:
            subs = geo.parse_region(region)
            if subs:
                box = geo.bbox_of_subs(subs)
                out["region"] = {"bbox": [box.x, box.y, box.w, box.h],
                                 "areas": len(subs)}

        goto = X.find(el, "Goto")
        if goto is not None:
            dest = X.find(goto, "Dest")
            bm = X.find(goto, "Bookmark")
            if bm is not None:
                out["kind"] = "goto"
                out["bookmark"] = bm.get("Name") or ""
            elif dest is not None:
                out["kind"] = "goto"
                # 兼容旧前端：dest 仍是页面 ID 字符串，细节放 dest_detail
                out["dest"] = dest.get("PageID") or dest.get("Type") or ""
                out["dest_detail"] = extras.parse_dest(dest)

        uri = X.find(el, "URI")
        if uri is not None:
            out["kind"] = "uri"
            out["uri"] = uri.get("URI") or ""
            out["base"] = uri.get("Base") or ""
            # 附录 A 的 URI 动作还带一个 Target（打开的目标窗口名），正文表56 没写
            out["target"] = uri.get("Target") or ""

        ga = X.find(el, "GotoA")
        if ga is not None:
            out["kind"] = "gotoa"
            out["attach_id"] = ga.get("AttachID") or ""
            out["new_window"] = X.bool_attr(ga.get("NewWindow"), True)

        snd = X.find(el, "Sound")
        if snd is not None:
            out["kind"] = "sound"
            out["resource_id"] = snd.get("ResourceID") or ""
            out["volume"] = int(X.fnum(snd.get("Volume"), 100))
            out["repeat"] = X.bool_attr(snd.get("Repeat"), False)
            out["synchronous"] = X.bool_attr(snd.get("Synchronous"), False)

        mv = X.find(el, "Movie")
        if mv is not None:
            out["kind"] = "movie"
            out["resource_id"] = mv.get("ResourceID") or ""
            out["operator"] = mv.get("Operator") or "Play"

        if "kind" not in out:
            out["kind"] = "unknown"
        return out

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
        # 第 13 章：矢量图像资源（CT_VectorG），复合对象的内容在这里。
        # {ID: {"w":..., "h":..., "content": <Element 或 None>, "thumb":..., "sub":...}}
        self.vectorgs: Dict[str, dict] = {}

    def load(self) -> None:
        root = self.doc.pkg.parse_xml(self.entry)
        if root is None:
            return
        self.root = root
        self.base_loc = (root.get("BaseLoc") or "").strip()
        # 表 50：矢量图像 CT_VectorG —— Width/Height 是裁剪边界，Content 是页面块
        # 元素名按标准/附录 A 是 CompositeGraphicUnit（注意 Graphic 里的 ic）；
        # 早期本项目写成了 CompositeGraphUnit，标准件一个都读不到（见 docs 核对记录）。
        for vel in list(X.deepfind_all(root, "CompositeGraphicUnit")) + \
                list(X.deepfind_all(root, "CompositeGraphUnit")):
            vid = vel.get("ID") or ""
            if not vid:
                continue
            cel = X.find(vel, "Content")
            self.vectorgs[vid] = {
                "w": X.fnum(vel.get("Width"), 0.0),
                "h": X.fnum(vel.get("Height"), 0.0),
                "content": cel,
                "thumb": (X.find(vel, "Thumbnail") is not None),
                "sub": (X.find(vel, "Substitution") is not None),
            }

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
                X.bool_attr(fel.get("FixedWidth"), False),
            )
        # 表25：颜色空间带 Type / BitsPerComponent / Palette(CV)，
        # 颜色取值要靠这三样才能算对（8.3.2），所以连调色板一起存。
        for cel in X.deepfind_all(root, "ColorSpace"):
            cid = cel.get("ID") or ""
            if not cid:
                continue
            palette = []
            for cv in X.deepfind_all(cel, "CV"):
                vals = X.fnum_list(cv.text or "")
                if vals:
                    palette.append(vals)
            self.colorspaces[cid] = {
                "type": cel.get("Type") or cel.get("Name") or "",
                "bpc": int(X.fnum(cel.get("BitsPerComponent"), 8)) or 8,
                "palette": palette,
            }
        # DrawParam 的标准位置是 PublicRes/DrawParams，DocumentRes 里也可能有
        for dp in X.deepfind_all(root, "DrawParam"):
            did = dp.get("ID") or ""
            if did:
                self.drawparams[did] = parse_draw_param(dp, self.doc)

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
                 "embedded", "owner", "fixed_width")

    def __init__(self, fid, name, family, charset, serif, bold, italic, embedded,
                 owner, fixed_width=False):
        self.id = fid
        self.name = name
        self.family = family
        self.charset = charset
        self.serif = serif
        self.bold = bold
        self.italic = italic
        self.embedded = embedded
        self.owner = owner
        # 表44 FixedWidth：true 表示这是等宽字型（每个字形都占满 em）
        self.fixed_width = bool(fixed_width)

    def display_name(self) -> str:
        return self.family or self.name or ("Font-%s" % self.id)

    def __repr__(self):
        return "FontSpec(%s, %s)" % (self.id, self.display_name())


def parse_draw_param(el, doc=None) -> dict:
    """把一个 <DrawParam> 元素解析成属性字典。

    颜色是子元素：
        <ofd:DrawParam ID="8" LineWidth="0.25">
          <ofd:FillColor Value="128 0 0" ColorSpace="5"/>
          <ofd:StrokeColor Value="128 0 0" ColorSpace="5"/>
        </ofd:DrawParam>

    表21 的全部属性都在这里：Relative / Join / LineWidth / DashOffset /
    DashPattern / Cap / MiterLimit / FillColor / StrokeColor。
    Relative 的继承由 Document.find_drawparam 负责合并，这里只记录引用。
    """
    info = {
        "Relative": el.get("Relative"),
        "LineWidth": None,
        "Join": el.get("Join"),
        "Cap": el.get("Cap"),
        "DashPattern": el.get("DashPattern"),
        "DashOffset": None,
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
    do = el.get("DashOffset")
    if do is not None and str(do).strip() != "":
        info["DashOffset"] = X.fnum(do, None)
    for tag, key in (("FillColor", "FillColor"), ("StrokeColor", "StrokeColor")):
        child = X.find(el, tag)
        if child is not None:
            info[key] = parse_color_el(child, doc, None)
    return info


def parse_color_el(el, doc=None, default=None):
    """解析一个 CT_Color 元素（<FillColor>/<StrokeColor>/BorderColor…）。

    表26：Value 与 Index 二选一；ColorSpace 是资源里的颜色空间 ID，
    要连同类（GRAY/RGB/CMYK）、BPC、调色板一起解析，否则 BPC≠8 或
    用了调色板的文件会整片颜色错。
    """
    if el is None:
        return default
    value = el.get("Value")
    if value is None:
        value = el.get("Color")
        if value is None and (el.text or "").strip():
            value = el.text
    cs_id = el.get("ColorSpace")
    info = doc.find_colorspace(cs_id) if doc is not None else None
    return colorlib.parse_color(
        value,
        (info or {}).get("type") or cs_id,
        el.get("Alpha"),
        default,
        bpc=(info or {}).get("bpc", 8),
        palette=(info or {}).get("palette"),
        index=el.get("Index"),
    )


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
                self.drawparams[did] = parse_draw_param(dp, self.doc)

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

    def _area_box(self, name: str):
        """读页面 Area 下的某个区域（表 7）。没有就返回 None。"""
        if self.content_root is None:
            return None
        area = X.find(self.content_root, "Area")
        if area is None:
            return None
        box = X.find(area, name)
        if box is None or not (box.text or "").strip():
            return None
        vals = X.fnum_list(box.text)
        if len(vals) < 4:
            return None
        return geo.Rect(vals[0], vals[1], vals[2], vals[3]).normalized()

    def physical_box(self):
        return self._area_box("PhysicalBox") or self.size()

    def application_box(self):
        """表 7：显示区域，页面内容实际显示或打印输出的区域。

        位于物理区域内；不完全在物理区内时，**物理区外的部分忽略**
        （即有效显示区 = ApplicationBox ∩ PhysicalBox）；完全在外则整页空白。
        """
        return self._area_box("ApplicationBox")

    def content_box(self):
        """表 7：版心区域，正文区域，位于显示区域内。

        例外同上：与显示区求交；完全在外则版心内容不绘制。
        """
        return self._area_box("ContentBox")

    def bleed_box(self):
        """表 7：出血区域，位于物理区域外；不出现时默认等于物理区。"""
        return self._area_box("BleedBox")

    def show_box(self):
        """实际用于显示的矩形（三段求交），返回 (rect, blank)。

        blank=True 表示完全落在物理区外，整页应画成空白。
        """
        phys = self.physical_box()
        cur = phys
        for getter in (self.application_box, self.content_box):
            b = getter()
            if b is None:
                continue
            # Rect 没有内置交集，手算（几何库里只有 union/contains/intersects_box）
            x0 = max(cur.x, b.x)
            y0 = max(cur.y, b.y)
            x1 = min(cur.x + cur.w, b.x + b.w)
            y1 = min(cur.y + cur.h, b.y + b.h)
            if x1 <= x0 or y1 <= y0:
                return phys, True      # 完全不相交 → 整页空白
            cur = geo.Rect(x0, y0, x1 - x0, y1 - y0)
        return cur, False

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

    def all_layers(self, for_print: bool = False) -> List["Layer"]:
        """按绘制顺序的完整图层列表。

        背景模板 → 本页图层 → 前景模板 → 注释。
        第15章把注释定义为"版式文档形成后附加的图文信息"，所以叠在最上面；
        签章（Stamp）就在这一层，漏掉它页面就少个章。

        表 61 的两个开关要看清作用对象：
          · Visible —— **注释对象**是否显示，默认 true → 控制整个 Appearance
          · Print   —— 对象的 **Remark 信息**是否随页面打印，默认 true
                        → 只管说明文字，**不影响 Appearance**
        本项目不渲染 Remark（它是注释说明，不属于页面内容），
        所以 Appearance 永远按 Visible 决定，不能因为 Print=false 跳过。
        for_print 保留给将来渲染 Remark 时用。
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
            # 只看 Visible：Print 管的是 Remark 文本，与外观无关（表 61）
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
        """图层里按顺序的对象，**含**GroupObject 的子对象。

        但 CompositeObject 的子对象**不能**在这里展开 —— 它的内容在
        ResourceID 指向的矢量图像里，且要以自己的 Boundary 为原点、按时
        自己的 Width/Height 裁剪。这些都要靠 scene._emit_object 处理；
        在这里提前拍平会让同一份内容被画两遍（一份没偏移、一份偏移过）。
        """
        out = []
        for o in self._objects:
            out.append(o)
            if isinstance(o, CompositeObject):
                continue
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
    def _cs_of(self, cs_id):
        """颜色空间属性是资源 ID（8.3.1），回资源里查 Type/BPC/调色板。"""
        return self.page.doc.find_colorspace(cs_id) if cs_id not in (None, "") else None

    def _color_attr(self, attr: str, cs_attr: str, default=None):
        raw = self.element.get(attr)
        cs = self.element.get(cs_attr) or self.element.get("ColorSpace")
        info = self._cs_of(cs)
        return colorlib.parse_color(
            raw, (info or {}).get("type") or cs, self.element.get("Alpha"), default,
            bpc=(info or {}).get("bpc", 8),
            palette=(info or {}).get("palette"),
            index=self.element.get("Index"))

    def _color_child(self, child_name: str):
        """颜色也可能写成子元素：<ofd:FillColor Value="128 0 0" ColorSpace="5"/>

        数电发票、部分公文生成器都用这种写法。只读属性会漏掉全部颜色。
        """
        el = X.find(self.element, child_name)
        if el is None:
            return None
        return parse_color_el(el, self.page.doc, None)

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
        # 只看标准里真实存在的三处依据：StrokeColor 子元素、LineWidth 属性、
        # DrawParam 引用（绘制参数对象里可能带描边）。
        # 早期这里还判断过一个 "StrokePattern" 元素——它不在标准里，
        # 属臆造，已删除（后果无害但会误导后续维护）。
        if X.find(self.element, "StrokeColor") is not None:
            return True
        if self.element.get("LineWidth") is not None:
            return True
        if self.element.get("DrawParam") is not None:
            return True
        return self.STROKE_DEFAULT

    def fill_shading(self):
        """FillColor 里装的是渐变/底纹时返回 Shading，否则 None。

        8.3.4：渐变被当作"颜色的一种"，写在 <FillColor> 容器里，
        与基本颜色互斥（CT_Color 是 xs:choice）。
        """
        node = X.find(self.element, "FillColor")
        if node is None:
            return None
        for kind in Shading.KINDS:
            sub = X.find(node, kind)
            if sub is not None:
                return Shading(kind, sub, self.page.doc)
        return None

    def stroke_shading(self):
        node = X.find(self.element, "StrokeColor")
        if node is None:
            return None
        for kind in Shading.KINDS:
            sub = X.find(node, kind)
            if sub is not None:
                return Shading(kind, sub, self.page.doc)
        return None

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
        # 表 21：LineWidth 默认值为 0.353mm（不是 0.35）
        w = self.element.get("LineWidth")
        if w is not None and str(w).strip() != "":
            return X.fnum(w, 0.353)
        dp = self._dp("LineWidth")
        return float(dp) if dp is not None else 0.353

    def line_join(self) -> str:
        """表 21：Join 可取 Miter / Round / Bevel，**默认值为 Miter**。

        绘制参数的"就近原则"（表 21 下方正文）：
          图元自身属性 > 图元引用的 DrawParam > 所在图层的 DrawParam > 默认值
        layer 的 DrawParam 在 draw_param() 里已经兜住，这里只管前两层 + 默认值。
        """
        return (self.element.get("Join") or self._dp("Join") or "miter").lower()

    def miter_limit(self):
        """8.2.3：Join 为 Miter 时小角度结合点长度的截断值。

        默认值取 **3.528**，跟标准正文（表21 明文「默认值为3.528」）。
        附录 A 的 Page.xsd 写的是 4.234，与正文冲突，属标准自身缺陷；
        业界先例也不统一：PDF 默认 10、SVG 默认 4。
        旁证：WPS 的 kufeofddom.dll 里有 3.5f 常量，与 3.528 吻合，
        而 4.234 在业界找不到对应，故按正文。
        仅当 Join=Miter 时生效（标准明文），其它联接样式下后端会忽略。
        """
        v = self.element.get("MiterLimit")
        if v not in (None, ""):
            return X.fnum(v, 3.528)
        # 也可能来自 DrawParam 引用的绘制参数
        for name in ("MiterLimit",):
            got = self._dp(name) if hasattr(self, "_dp") else None
            if got is not None:
                return float(got)
        return 3.528

    def line_cap(self) -> str:
        """表 21：Cap 可取 Butt / Round / Square，**默认值为 Butt**。"""
        return (self.element.get("Cap") or self._dp("Cap") or "butt").lower()

    def dash_offset(self) -> float:
        """8.2.4：虚线起始偏移。DashPattern 不出现时该参数无效。"""
        v = self.element.get("DashOffset")
        if v not in (None, ""):
            return X.fnum(v, 0.0)
        dv = self._dp("DashOffset")
        return float(dv) if dv is not None else 0.0

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

    # ---------------------------------------------------------- 裁剪区（8.4 + 表33/34）

    def actions(self):
        """图元对象的动作序列（14 章 / CT_Action，写在 CT_GraphicUnit 的 Actions 里）。

        返回 [{event, kind, ...}]，空列表表示没有动作。
        本项目只解析不执行交互——网页端可以据此显示可点击区域。
        """
        node = X.find(self.element, "Actions")
        if node is None:
            return []
        return [self.page.doc._action_desc(a) for a in X.find_all(node, "Action")]

    def clips(self):
        """返回裁剪区列表。空列表表示不裁剪。

        GB/T 33190-2016 的两层组合规则（不是一回事，别混）：

          · 表34：`Clips` 下有多个 `Clip` 对象 → 最终裁剪区是**交集**；
          · 表33：一个 `Clip` 下可有多个 `Area`（Path 或 Text）→ 是**并集**；
          · 8.1.4：裁剪区数据以**所修饰图元对象的外接矩形左上角**为原点。

        因此返回结构是「组」的列表：外层每个元素是一个 Clip（组间取交集），
        组内是若干子路径（组内取并集）。
        """
        node = X.find(self.element, "Clips")
        if node is None:
            return []
        groups = []
        for clip in X.find_all(node, "Clip"):
            # 表 33：Clip 自带的 CTM「针对对象坐标系，对 Area 下包含的 Path 和 Text
            # 进行进一步的变换」——必须叠加，否则带变换的裁剪区会裁错位置。
            cm_raw = clip.get("CTM")
            ccm = None
            if cm_raw:
                v = X.fnum_list(cm_raw)
                if len(v) >= 6:
                    ccm = (v[0], v[1], v[2], v[3], v[4], v[5])
            subs = []
            for area in list(clip):
                # Area 下二选一：Path 或 Text（schema 里是 xs:choice）
                path_el = X.find(area, "Path")
                if path_el is not None:
                    # 路径数据可能在 <AbbreviatedData> 子元素，也可能是 Path/@Data，
                    # 两种写法都认（与 PathObject 的读取逻辑一致）
                    data = ""
                    ab = X.find(path_el, "AbbreviatedData")
                    if ab is not None and (ab.text or "").strip():
                        data = ab.text.strip()
                    elif path_el.get("Data"):
                        data = path_el.get("Data").strip()
                    if data:
                        # 裁剪区里的 Path 是完整图形对象，自带 Boundary（必选）。
                        # 它的 AbbreviatedData 相对自己的 Boundary 左上角，
                        # 所以要加上这个偏移才能落到对象空间。
                        ob = X.fnum_list(path_el.get("Boundary") or "")
                        ox = ob[0] if len(ob) >= 4 else 0.0
                        oy = ob[1] if len(ob) >= 4 else 0.0
                        for arr in _abbrev_to_subpaths(data):
                            subs.append([arr[k] + ox if k % 2 == 0 else arr[k] + oy
                                         for k in range(len(arr))])
                text_el = X.find(area, "Text")
                if text_el is not None:
                    box = text_el.get("Boundary")
                    if box:
                        b = X.fnum_list(box)
                        if len(b) >= 4:
                            # 文字裁剪区按它的外接矩形近似（标准允许 Text 作裁剪区，
                            # 精确做法要按字形轮廓，这里退化为矩形，够用且不会错到别处）
                            subs.append([b[0], b[1], b[0] + b[2], b[1],
                                         b[0] + b[2], b[1] + b[3], b[0], b[1] + b[3]])
            if subs:
                groups.append((ccm, subs))
        return groups

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

    def weight(self) -> int:
        """表 45 的 Weight：100~900，默认 400。只认这几个档位。"""
        v = int(X.fnum(self.element.get("Weight"), 400))
        if v not in (100, 200, 300, 400, 500, 600, 700, 800, 900):
            # 非标准值按最近的 100 取整；<100 或 >900 夹到边界
            v = max(100, min(900, int(round(v / 100.0)) * 100))
        return v

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

    # ---------------------------------------------------------- 字形变换（11.4）

    def cg_transforms(self):
        """字形变换（11.4 / 表48）。

        `CGTransform` 与 `TextCode` 是 `CT_Text` 下**并列**的子元素，不是嵌在
        TextCode 里；`CodePosition` 指向 TextCode 里字符的起始位置，所以一个
        TextObject 可以有多个 CGTransform，分别对应不同的 TextCode。

        返回 [{code_position, code_count, glyph_count, glyphs}]，按 CodePosition 排序。
        """
        out = []
        for el in X.find_all(self.element, "CGTransform"):
            # 附录 A：Glyphs 是子元素（ST_Array），不是属性。
            # 表48 说它"必选"、schema 却是 minOccurs=0 —— 标准自相矛盾，
            # 这里按"有就解析、没有就跳过"处理。
            gel = X.find(el, "Glyphs")
            raw = (gel.text if gel is not None else None) or el.get("Glyphs") or ""
            glyphs = X.fnum_list(raw)
            if not glyphs:
                continue
            out.append({
                "code_position": int(X.fnum(el.get("CodePosition"), 0)),
                "code_count": max(1, int(X.fnum(el.get("CodeCount"), 1))),
                "glyph_count": max(1, int(X.fnum(el.get("GlyphCount"), 1))),
                "glyphs": [int(g) for g in glyphs],
            })
        out.sort(key=lambda d: d["code_position"])
        return out

    def has_nonuniform_glyphs(self) -> bool:
        """是否存在"字符数≠字形数"的变换（多对一 / 一对多 / 多对多）。"""
        for t in self.cg_transforms():
            if t["code_count"] != t["glyph_count"] or len(t["glyphs"]) != t["code_count"]:
                return True
        return False

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
        if not data:
            # 标准里非紧缩描述（表37-42）只用于区域；有的导出器把它写在
            # PathObject 下，读得动就读（写回仍按标准输出 AbbreviatedData）
            sp = geo.parse_area(self.element, m)
            return [sp] if sp is not None else []
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
        # 表 43 的拼写就是 ResouceID（少一个 r），两种都认
        return (self.element.get("ResourceID")
                or self.element.get("ResouceID") or "")

    def substitution_id(self) -> str:
        """表 43：可替换图像，高分辨率输出时用它替换主图。"""
        return self.element.get("Substitution") or ""

    def mask_id(self) -> str:
        """表 43：图像蒙版，应与主图同大小的二值图。"""
        return self.element.get("ImageMask") or ""

    def image_bytes(self) -> Optional[Tuple[str, bytes]]:
        return self.page.doc.find_image(self.resource_id())

    def image_bytes_of(self, media_id: str) -> Optional[Tuple[str, bytes]]:
        """按多媒体资源 ID 取图片字节（给 Substitution / ImageMask 用）。"""
        if not media_id:
            return None
        return self.page.doc.find_image(media_id)

    def _hit_local(self, lx: float, ly: float, tol: float) -> bool:
        return self.boundary().normalized().contains(lx, ly, tol)

    def describe(self) -> str:
        got = self.image_bytes()
        return "图片 #%s  %s" % (self.id, got[0] if got else "资源缺失")

    def image_border(self):
        """图像边框（表43 的 Border 子元素）。

        返回 None 表示不画边框。字段：
            line_width  线宽 mm，**为 0 表示不绘制边框**（表43 明确写了）
            radius_h / radius_v  水平 / 垂直角半径（标准拼写 HorizonalCornerRadius 少个 z）
            dash / dash_offset   边框虚线；起点在左上角、绕行方向顺时针
            color       边框颜色，默认黑色
        """
        node = X.find(self.element, "Border")
        if node is None:
            return None
        lw = node.get("LineWidth")
        width = X.fnum(lw, 0.353) if lw not in (None, "") else 0.353
        if width <= 0:
            return None                      # 表43：0 表示不绘制
        color = (0, 0, 0, 1)
        cel = X.find(node, "BorderColor")
        if cel is not None:
            color = parse_color_el(cel, self.page.doc, (0, 0, 0, 1)) or color
        dp = node.get("DashPattern")
        dash = X.fnum_list(dp) if dp and dp != "0" else None
        if dash and all(v <= 0 for v in dash):
            dash = None
        return {
            "w": width,
            "radius_h": X.fnum(node.get("HorizonalCornerRadius"), 0.0),
            "radius_v": X.fnum(node.get("VerticalCornerRadius"), 0.0),
            "dash": dash,
            "dash_offset": X.fnum(node.get("DashOffset"), 0.0),
            "color": color,
        }


@register_object("CompositeObject")

class CompositeObject(OFDObject):
    """组合对象：内部再挂若干对象，可能有自己的 CTM。"""

    TYPE = "组合"

    def resource_id(self) -> str:
        """表 49：复合对象的内容在 ResourceID 指向的矢量图像资源里。"""
        return (self.element.get("ResourceID")
                or self.element.get("ResouceID") or "")

    def vector_g(self):
        vid = self.resource_id()
        if not vid:
            return None
        return self.page.doc.find_vectorg(vid)

    def content_objects(self) -> List["OFDObject"]:
        """把矢量图像资源里的 Content（CT_PageBlock）展开成对象列表。"""
        vg = self.vector_g()
        if not vg or vg.get("content") is None:
            return []
        out = []
        for ch in list(vg["content"]):
            cls = _OBJ_TYPES.get(X.local(ch.tag))
            if cls is not None:
                out.append(cls(self.page, ch, self.layer))
        return out

    def children(self) -> List["OFDObject"]:
        # 优先用 ResourceID 指向的矢量图像内容（第 13 章的正规定义）；
        # 没有 ResourceID 时才退回把 XML 子元素当内容（兼容非标准写法）。
        via_res = self.content_objects()
        if via_res:
            return via_res
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

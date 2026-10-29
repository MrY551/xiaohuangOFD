# -*- coding: utf-8 -*-
"""编辑操作 + 撤销/重做。

所有编辑都走 Editor 的方法，不直接改 DOM。这样：
  * 每次操作前后留快照，撤销/重做可靠
  * 保存时只有脏条目被重写，其他字节不动
  * 网页端与桌面端调同一套方法

快照粒度：
  * 页内对象操作 → 该页 Content.xml 的深拷贝
  * 页面增删/换序 → 整包条目表的浅拷贝（字节引用，不复制内容）
"""
from __future__ import annotations

import copy
import io
import os
import zipfile
from typing import Dict, List, Optional, Sequence, Tuple

from . import colors as colorlib
from . import geometry as geo
from . import model
from . import xmlutil as X
from .errors import OfdError, ParseError


class EditError(OfdError):
    pass


class _Action(object):
    """一次可撤销操作。"""

    __slots__ = ("label", "kind", "page", "before", "after", "pkg",
                 "entries_before", "entries_after", "extra_before", "extra_after",
                 "pages")

    def __init__(self, label: str, kind: str, page=None, before=None, after=None,
                 pkg=None, entries_before=None, entries_after=None):
        self.label = label
        self.kind = kind            # "page" | "package"
        self.page = page
        self.before = before
        self.after = after
        self.pkg = pkg
        self.entries_before = entries_before
        self.entries_after = entries_after
        # 页级操作顺手改了包级 XML（比如插图片要改 DocumentRes.xml）时，
        # 用字节快照兜住，撤销时回填原始字节，保证"没改的条目字节不变"
        self.extra_before = []
        self.extra_after = []
        self.pages = []


class Editor(object):
    """文档编辑器。持有一个 OFDPackage。"""

    def __init__(self, pkg: model.OFDPackage, max_undo: int = 100):
        self.pkg = pkg
        self.undo_stack: List[_Action] = []
        self.redo_stack: List[_Action] = []
        self.max_undo = max_undo
        self.dirty = False
        self.current_page_index = 0
        self._pending = None

    # ---------------------------------------------------------- 快照
    def _page_snap(self, page: model.Page):
        return page.content_snapshot()

    def _pkg_entries_snap(self) -> Dict[str, bytes]:
        """整包快照。先把脏的 XML 树写回 entries，保证快照是最新状态。"""
        for name in list(self.pkg.dirty):
            root = self.pkg.xml_roots.get(name)
            if root is not None:
                self.pkg.entries[name] = X.tostring(root)
        return dict(self.pkg.entries)

    def _begin_page(self, page: model.Page) -> object:
        return self._page_snap(page)

    def _snap_entries(self, names) -> list:
        """字节级快照：[(条目名, 原始字节或 None)]。None 表示当时不存在。"""
        out = []
        for n in names:
            if not n:
                continue
            out.append((n, self.pkg.entries.get(n)))
        return out

    def _restore_entries(self, snaps) -> None:
        """把包内条目字节恢复成快照，并让相关 XML 缓存失效。"""
        if not snaps:
            return
        for name, data in snaps:
            if data is None:
                if name in self.pkg.entries:
                    del self.pkg.entries[name]
            else:
                self.pkg.entries[name] = data
            self.pkg.xml_roots.pop(name, None)
            self.pkg.dirty.discard(name)
        # 资源索引要跟着重建
        for doc in self.pkg.documents:
            for res in doc.public_res:
                res.load()
            if doc.doc_res is not None:
                doc.doc_res.load()

    def _end_page(self, page: model.Page, before, label: str,
                  extra_names=None, extra_before=None) -> None:
        after = self._page_snap(page)
        act = _Action(label, "page", page=page, before=before, after=after)
        if extra_names:
            act.extra_before = extra_before if extra_before is not None \
                else self._snap_entries(extra_names)
            act.extra_after = self._snap_entries(extra_names)
        self._push(act)
        self.dirty = True

    def _push(self, action: _Action) -> None:
        self.undo_stack.append(action)
        if len(self.undo_stack) > self.max_undo:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    # ---------------------------------------------------------- 撤销/重做
    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    def undo_label(self) -> str:
        return self.undo_stack[-1].label if self.undo_stack else ""

    def redo_label(self) -> str:
        return self.redo_stack[-1].label if self.redo_stack else ""

    def undo(self) -> Optional[str]:
        if not self.undo_stack:
            return None
        act = self.undo_stack.pop()
        if act.kind == "page" and act.page is not None:
            act.page.restore_snapshot(act.before)
            self._restore_entries(act.extra_before)
        elif act.kind == "pages":
            for p, snap in zip(act.pages, act.before):
                p.restore_snapshot(snap)
        elif act.kind == "package":
            self.pkg.entries = dict(act.entries_before)
            self._reload_after_package_change()
        self.redo_stack.append(act)
        self.dirty = True
        return act.label

    def redo(self) -> Optional[str]:
        if not self.redo_stack:
            return None
        act = self.redo_stack.pop()
        if act.kind == "page" and act.page is not None:
            act.page.restore_snapshot(act.after)
            self._restore_entries(act.extra_after)
        elif act.kind == "pages":
            for p, snap in zip(act.pages, act.after):
                p.restore_snapshot(snap)
        elif act.kind == "package":
            self.pkg.entries = dict(act.entries_after)
            self._reload_after_package_change()
        self.undo_stack.append(act)
        self.dirty = True
        return act.label

    def _reload_after_package_change(self) -> None:
        """整包条目变化后重建模型（页增删场景）。

        重建会让旧的 Page 对象全部失效，所以撤销栈里引用了旧页的
        页级操作要标记作废（page=None），避免对已脱离文档的对象做还原。
        """
        path = self.pkg.path
        try:
            buf = io.BytesIO()
            names = list(self.pkg.entries.keys())
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for n in names:
                    zf.writestr(n, self.pkg.entries[n])
            new_pkg = model.OFDPackage.from_bytes(buf.getvalue(), path or "<memory>")
            self.pkg.entries = new_pkg.entries
            self.pkg.xml_roots = new_pkg.xml_roots
            self.pkg.ofd_root = new_pkg.ofd_root
            self.pkg.documents = new_pkg.documents
            self.pkg._entry_order = new_pkg._entry_order
            self.pkg.dirty = set()          # entries 已是最新字节，无需再序列化
            self.pkg.path = path
            # 关键：把新解析出来的 Document/Page/Res 重新挂到当前 pkg 上。
            # 否则它们内部的 pkg 引用还指向临时对象，touch() 会打到不存在的地方，
            # 表现为"编辑生效但保存没写进去"。
            for d in self.pkg.documents:
                d.pkg = self.pkg
                for p in d.pages:
                    p.doc = d
                for r in d.public_res:
                    r.doc = d
                if d.doc_res is not None:
                    d.doc_res.doc = d
        except Exception as exc:
            raise EditError("重建文档失败: %s" % exc)
        for a in list(self.undo_stack) + list(self.redo_stack):
            if a.kind == "page":
                a.page = None

    # ---------------------------------------------------------- 文本
    def set_text(self, obj: model.TextObject, text: str) -> None:
        if not isinstance(obj, model.TextObject):
            raise EditError("不是文本对象")
        if obj.text() == text:
            return
        page = obj.page
        before = self._begin_page(page)
        obj.set_text(text)
        self._end_page(page, before, "修改文本")

    def set_text_style(self, obj: model.TextObject, size_mm: Optional[float] = None,
                       bold: Optional[bool] = None, italic: Optional[bool] = None,
                       color=None, font_id: Optional[str] = None,
                       spacing: Optional[float] = None) -> None:
        page = obj.page
        before = self._begin_page(page)
        el = obj.element
        if size_mm is not None and size_mm > 0:
            el.set("Size", geo._fmt(size_mm))
        if bold is not None:
            el.set("Weight", "700" if bold else "400")
        if italic is not None:
            el.set("Italic", "true" if italic else "false")
        if color is not None:
            cs, val, alpha = colorlib.to_ofd_value(color)
            el.set("FillColor", val)
            el.set("ColorSpace", cs)
            el.set("Fill", "true")
            if alpha != "255":
                el.set("Alpha", alpha)
        if font_id:
            el.set("Font", str(font_id))
        if spacing is not None:
            el.set("CharSpacing", geo._fmt(spacing))
        self.pkg.touch(page.content_entry)
        self._end_page(page, before, "修改文字样式")

    def replace_all(self, find: str, repl: str,
                    page_indexes: Optional[Sequence[int]] = None) -> int:
        """批量查找替换，返回替换次数。整次操作压成一个撤销单元。"""
        if not find:
            return 0
        pages = self.pkg.all_pages()
        targets = [pages[i] for i in page_indexes if 0 <= i < len(pages)] \
            if page_indexes is not None else pages
        snaps = {id(p): self._begin_page(p) for p in targets}
        n = 0
        for page in targets:
            for obj in page.objects():
                if not isinstance(obj, model.TextObject):
                    continue
                t = obj.text()
                if find in t:
                    n += t.count(find)
                    obj.set_text(t.replace(find, repl))
        if n:
            act = _Action("查找替换", "pages")
            act.pages = list(targets)
            act.before = [snaps[id(p)] for p in targets]
            act.after = [p.content_snapshot() for p in targets]
            self._push(act)
            self.dirty = True
        return n

    # ---------------------------------------------------------- 几何
    def move(self, objs: Sequence[model.OFDObject], dx: float, dy: float) -> None:
        if not objs or (abs(dx) < 1e-9 and abs(dy) < 1e-9):
            return
        page = objs[0].page
        before = self._begin_page(page)
        tr = geo.Matrix.translate(dx, dy)
        for o in objs:
            o.set_ctm(tr.mul(o.ctm()))
        self._end_page(page, before, "移动对象")

    def scale(self, objs: Sequence[model.OFDObject], sx: float, sy: float,
              ax: float, ay: float) -> None:
        if not objs or (abs(sx - 1) < 1e-9 and abs(sy - 1) < 1e-9):
            return
        page = objs[0].page
        before = self._begin_page(page)
        m = (geo.Matrix.translate(ax, ay).mul(geo.Matrix.scale(sx, sy))
             .mul(geo.Matrix.translate(-ax, -ay)))
        for o in objs:
            o.set_ctm(m.mul(o.ctm()))
        self._end_page(page, before, "缩放对象")

    def rotate(self, objs: Sequence[model.OFDObject], deg: float,
               ax: float, ay: float) -> None:
        if not objs or abs(deg) < 1e-9:
            return
        page = objs[0].page
        before = self._begin_page(page)
        m = (geo.Matrix.translate(ax, ay).mul(geo.Matrix.rotate(deg))
             .mul(geo.Matrix.translate(-ax, -ay)))
        for o in objs:
            o.set_ctm(m.mul(o.ctm()))
        self._end_page(page, before, "旋转对象")

    def set_fill(self, objs: Sequence[model.OFDObject], color) -> None:
        if not objs:
            return
        page = objs[0].page
        before = self._begin_page(page)
        cs, val, alpha = colorlib.to_ofd_value(color)
        for o in objs:
            o.element.set("Fill", "true")
            o.element.set("FillColor", val)
            o.element.set("ColorSpace", cs)
            if alpha != "255":
                o.element.set("Alpha", alpha)
        self.pkg.touch(page.content_entry)
        self._end_page(page, before, "修改填充色")

    def set_stroke(self, objs: Sequence[model.OFDObject], color,
                   width: Optional[float] = None) -> None:
        if not objs:
            return
        page = objs[0].page
        before = self._begin_page(page)
        cs, val, alpha = colorlib.to_ofd_value(color)
        for o in objs:
            o.element.set("Stroke", "true")
            o.element.set("StrokeColor", val)
            o.element.set("ColorSpace", cs)
            if width is not None and width > 0:
                o.element.set("LineWidth", geo._fmt(width))
        self.pkg.touch(page.content_entry)
        self._end_page(page, before, "修改描边")

    def set_alpha(self, objs: Sequence[model.OFDObject], alpha01: float) -> None:
        if not objs:
            return
        page = objs[0].page
        before = self._begin_page(page)
        v = str(int(round(max(0.0, min(1.0, alpha01)) * 255)))
        for o in objs:
            o.element.set("Alpha", v)
        self.pkg.touch(page.content_entry)
        self._end_page(page, before, "修改透明度")

    # ---------------------------------------------------------- 增删对象
    def add_text(self, page: model.Page, x: float, y: float, text: str,
                 size_mm: float = 5.0, font_id: Optional[str] = None,
                 color=(0, 0, 0, 1), bold: bool = False) -> model.TextObject:
        """在页面坐标 (x, y) 处加一段文本，(x,y) 是基线左端。"""
        before = self._begin_page(page)
        nid = self.pkg.next_id()
        width = max(size_mm * 1.0, len(text) * size_mm * (1.0 if _is_cjk(text) else 0.55))
        rect = geo.Rect(x, y - size_mm, width, size_mm * 1.4)
        el = X.new_element("TextObject", ID=nid, Boundary=rect.to_str(),
                           Font=font_id or self._default_font_id(page),
                           Size=geo._fmt(size_mm),
                           Fill="true", ColorSpace="DeviceRGB",
                           Weight="700" if bold else "400", Stroke="false")
        cs, val, _a = colorlib.to_ofd_value(color)
        el.set("FillColor", val)
        el.set("ColorSpace", cs)
        tc = X.append_new(el, "TextCode", X="0", Y=geo._fmt(size_mm))
        tc.set("Text", text)
        layer = page.layers[-1] if page.layers else page.add_layer()
        layer.element.append(el)
        self.pkg.touch(page.content_entry)
        page.load_contents()
        self._end_page(page, before, "插入文本")
        return self._find_obj(page, nid)

    def add_path(self, page: model.Page, subpaths: List[str], stroke=(0, 0, 0, 1),
                 fill=None, width_mm: float = 0.4) -> model.OFDObject:
        """subpaths 是页面坐标的 SVG 风格路径串（M/L/C/Z）。"""
        before = self._begin_page(page)
        nid = self.pkg.next_id()
        subs = []
        for d in subpaths:
            subs.extend(geo.parse_path_data(d))
        bb = geo.bbox_of_subs(subs)
        rect = geo.Rect(bb.x, bb.y, max(bb.w, 0.1), max(bb.h, 0.1))
        el = X.new_element("PathObject", ID=nid, Boundary=rect.to_str(),
                           LineWidth=geo._fmt(width_mm), Stroke="true",
                           StrokeColor="0 0 0", ColorSpace="DeviceRGB")
        if fill is not None:
            cs, val, _a = colorlib.to_ofd_value(fill)
            el.set("Fill", "true")
            el.set("FillColor", val)
            el.set("ColorSpace", cs)
        else:
            el.set("Fill", "false")
        if stroke is not None:
            cs, val, _a = colorlib.to_ofd_value(stroke)
            el.set("Stroke", "true")
            el.set("StrokeColor", val)
        # 路径数据折线化后转成相对 Boundary 原点的坐标
        rel = _subs_to_rel_data(subs, bb.x, bb.y)
        p = X.append_new(el, "Path")
        p.set("Data", rel)
        layer = page.layers[-1] if page.layers else page.add_layer()
        layer.element.append(el)
        self.pkg.touch(page.content_entry)
        page.load_contents()
        self._end_page(page, before, "插入图形")
        return self._find_obj(page, nid)

    def add_image(self, page: model.Page, data: bytes, name: str,
                  x: float, y: float, w: float, h: float) -> model.OFDObject:
        """插入图片。会往包里写文件并登记到 DocumentRes.xml。"""
        before = self._begin_page(page)
        doc = page.doc
        # DocumentRes.xml 与 Document.xml 会被改，先留字节快照，撤销时原样回填
        res_entry = (doc.doc_res.entry if doc.doc_res is not None
                     else doc.pkg.resolve(doc.entry, "DocumentRes.xml"))
        extra_before = self._snap_entries([res_entry, doc.entry])
        # 资源目录：跟着 DocumentRes.xml 的 BaseLoc 走
        base_dir = "Res"
        if doc.doc_res is not None and doc.doc_res.base_loc:
            base_dir = doc.doc_res.base_loc.strip("/")
        safe = _safe_name(name)
        entry = doc.pkg.resolve(doc.entry, "%s/%s" % (base_dir, safe))
        n = 1
        while doc.pkg.has(entry):
            base, ext = os.path.splitext(safe)
            safe = "%s_%d%s" % (base, n, ext)
            entry = doc.pkg.resolve(doc.entry, "%s/%s" % (base_dir, safe))
            n += 1
        doc.pkg.add_entry(entry, data)

        # DocumentRes.xml
        if doc.doc_res is None or doc.doc_res.root is None:
            from .model import DocumentRes
            res_entry = doc.pkg.resolve(doc.entry, "DocumentRes.xml")
            root = X.new_element("Res", BaseLoc=base_dir)
            doc.pkg.add_entry(res_entry, X.tostring(root))
            doc.pkg.xml_roots[res_entry] = root
            doc.doc_res = DocumentRes(doc, res_entry)
            doc.doc_res.root = root
            doc.doc_res.base_loc = base_dir
            parent = X.find(doc.root, "CommonData")
            if parent is None:
                parent = doc.root
            X.append_new(parent, "DocumentRes").text = os.path.basename(res_entry)
            doc.pkg.touch(doc.entry)
        res_root = doc.doc_res.root
        mms = X.find(res_root, "MultiMedias")
        if mms is None:
            mms = X.append_new(res_root, "MultiMedias")
        rid = self.pkg.next_id()
        mm = X.append_new(mms, "MultiMedia", ID=rid, Type="Image")
        X.append_new(mm, "MediaFile").text = safe
        doc.pkg.touch(doc.doc_res.entry)
        doc.doc_res.multimedia[rid] = safe

        nid = self.pkg.next_id()
        el = X.new_element("ImageObject", ID=nid,
                           Boundary=geo.Rect(x, y, w, h).to_str(),
                           ResourceID=rid, CTM="1 0 0 1 0 0")
        layer = page.layers[-1] if page.layers else page.add_layer()
        layer.element.append(el)
        self.pkg.touch(page.content_entry)
        page.load_contents()
        self._end_page(page, before, "插入图片",
                       extra_names=[res_entry, doc.entry], extra_before=extra_before)
        return self._find_obj(page, nid)

    def delete_objects(self, objs: Sequence[model.OFDObject]) -> int:
        if not objs:
            return 0
        page = objs[0].page
        before = self._begin_page(page)
        n = 0
        for o in objs:
            if page.remove_object(o):
                n += 1
        if n:
            page.load_contents()
            self._end_page(page, before, "删除对象")
        return n

    def duplicate_objects(self, objs: Sequence[model.OFDObject],
                          dx: float = 5.0, dy: float = 5.0) -> List[model.OFDObject]:
        if not objs:
            return []
        page = objs[0].page
        before = self._begin_page(page)
        new_ids = []
        layer = page.layers[-1] if page.layers else page.add_layer()
        for o in objs:
            el = X.clone(o.element)
            nid = self.pkg.next_id()
            el.set("ID", nid)
            ctm = geo.Matrix.parse(el.get("CTM"))
            el.set("CTM", geo.Matrix.translate(dx, dy).mul(ctm).to_str())
            layer.element.append(el)
            new_ids.append(nid)
        self.pkg.touch(page.content_entry)
        page.load_contents()
        self._end_page(page, before, "复制对象")
        return [self._find_obj(page, i) for i in new_ids]

    # ---------------------------------------------------------- 页面
    def insert_page(self, index: int, size: Optional[geo.Rect] = None) -> model.Page:
        snap = self._pkg_entries_snap()
        page = self.pkg.document.insert_page(index, size)
        self._push(_Action("插入页面", "package", pkg=self.pkg,
                           entries_before=snap,
                           entries_after=self._pkg_entries_snap()))
        self.dirty = True
        return page

    def delete_page(self, index: int) -> bool:
        if not (0 <= index < len(self.pkg.all_pages())):
            return False
        if len(self.pkg.all_pages()) <= 1:
            raise EditError("至少保留一页")
        snap = self._pkg_entries_snap()
        ok = self.pkg.document.remove_page(index)
        if ok:
            self._push(_Action("删除页面", "package", pkg=self.pkg,
                               entries_before=snap,
                               entries_after=self._pkg_entries_snap()))
            self.dirty = True
        return ok

    def move_page(self, src: int, dst: int) -> bool:
        snap = self._pkg_entries_snap()
        ok = self.pkg.document.move_page(src, dst)
        if ok:
            self._push(_Action("调整页序", "package", pkg=self.pkg,
                               entries_before=snap,
                               entries_after=self._pkg_entries_snap()))
            self.dirty = True
        return ok

    def set_page_size(self, page: model.Page, w_mm: float, h_mm: float) -> None:
        before = self._begin_page(page)
        page.set_size(geo.Rect(0, 0, w_mm, h_mm))
        self._end_page(page, before, "修改页面尺寸")

    def set_page_rotation(self, page: model.Page, deg: int) -> None:
        snap = self._pkg_entries_snap()
        page.set_rotation(deg)
        self._push(_Action("旋转页面", "package", pkg=self.pkg,
                           entries_before=snap,
                           entries_after=self._pkg_entries_snap()))
        self.dirty = True

    # ---------------------------------------------------------- 工具
    def _default_font_id(self, page: model.Page) -> str:
        for res in page.doc.public_res:
            if res.fonts:
                return sorted(res.fonts.keys())[0]
        return "1"

    def _find_obj(self, page: model.Page, obj_id: str) -> Optional[model.OFDObject]:
        for o in page.objects():
            if o.id == obj_id:
                return o
        return None

    def save(self, path: Optional[str] = None) -> str:
        out = self.pkg.save(path)
        self.dirty = False
        self.undo_stack.clear()
        self.redo_stack.clear()
        return out


def _is_cjk(text: str) -> bool:
    return any(ord(c) > 0x2E80 for c in text)


def _safe_name(name: str) -> str:
    base = os.path.basename(name or "image.png")
    out = []
    for ch in base:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out).strip("._") or "image"
    if "." not in s:
        s += ".png"
    return s


def _subs_to_rel_data(subs, ox: float, oy: float) -> str:
    """把子路径压成 OFD 的 AbbreviatedData（折线 L 指令），坐标减去 Boundary 原点。"""
    parts = []
    for sp in subs:
        if not sp.points:
            continue
        x0, y0 = sp.points[0]
        parts.append("M %s %s" % (geo._fmt(x0 - ox), geo._fmt(y0 - oy)))
        for x, y in sp.points[1:]:
            parts.append("L %s %s" % (geo._fmt(x - ox), geo._fmt(y - oy)))
        if sp.closed:
            parts.append("Z")
    return " ".join(parts)

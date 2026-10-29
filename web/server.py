#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OFD 网页端服务。

本地 HTTP 服务，浏览器里读/改/打印 OFD。桌面端与它共用 ofdlite 核心库，
渲染指令协议一致：服务端出 JSON 场景，浏览器 Canvas2D 执行。

    python web/server.py --port 8760 --open out/sample.ofd
    浏览器打开 http://127.0.0.1:8760

只监听 127.0.0.1，不对外。没有账号体系，本机单人使用。
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import posixpath
import socketserver
import sys
import tempfile
import threading
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofdlite import OFDPackage, convert, extras, font_manager, model
from ofdlite import geometry as geo
from ofdlite.edits import Editor, EditError
from ofdlite.errors import OfdError
from ofdlite.scene import build_page_scene, page_outline, selection_boxes

STATIC_DIR = os.path.join(HERE, "static")
MAX_UPLOAD = 200 * 1024 * 1024


class Session(object):
    """单个文档的编辑会话。多请求共享，需加锁。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.pkg = None
        self.editor = None
        self.page_index = 0
        self.tmpdir = tempfile.mkdtemp(prefix="ofdweb-")
        self.recent_warnings = []

    # ---------------------------------------------------------- 开关
    def open_path(self, path: str) -> dict:
        with self.lock:
            pkg = OFDPackage.open(path)
            self.pkg = pkg
            self.editor = Editor(pkg)
            self.page_index = 0
            return self.describe()

    def open_bytes(self, data: bytes, name: str) -> dict:
        with self.lock:
            safe = os.path.basename(name or "upload.ofd")
            target = os.path.join(self.tmpdir, safe)
            n = 1
            base, ext = os.path.splitext(target)
            while os.path.exists(target):
                target = "%s_%d%s" % (base, n, ext)
                n += 1
            with open(target, "wb") as fh:
                fh.write(data)
            return self.open_path(target)

    def require(self):
        if self.pkg is None:
            raise EditError("还没有打开文档")
        return self.pkg

    # ---------------------------------------------------------- 描述
    @staticmethod
    def _outlines(pkg):
        """7.8 大纲树。任何一步失败都退回空数组，不让它拖垮整页加载。"""
        try:
            return pkg.document.outlines()
        except Exception:
            return []

    def describe(self) -> dict:
        pkg = self.require()
        rep = convert.structure_report(pkg)
        pages = []
        for i, p in enumerate(pkg.all_pages()):
            pages.append({
                "index": i,
                "id": p.id,
                "w": round(p.size().w, 2),
                "h": round(p.size().h, 2),
                "rotation": p.rotation(),
                "objects": len(p.objects()),
            })
        fonts = {}
        for d in pkg.documents:
            for r in d.public_res:
                for fid, f in r.fonts.items():
                    fonts[fid] = f.display_name()
        return {
            "path": pkg.path,
            "title": pkg.document.title,
            # 7.8 大纲：解析成树给前端显示（有就显示，没有就空数组）
            "outlines": self._outlines(pkg),
            "author": pkg.document.author,
            "pages": pages,
            "fonts": fonts,
            "entries": rep["entries"],
            # ---- 7.5 / 16 / 17 / 18 / 19 / 20 章：以前只给大纲，现在全给前端
            "doc_type": pkg.doc_type,
            "ofd_version": pkg.ofd_version,
            "permissions": pkg.document.permissions,
            "vpreferences": pkg.document.vpreferences,
            "bookmarks": pkg.document.bookmarks,
            "attachments": pkg.document.attachments,
            "custom_tags": pkg.document.custom_tags,
            "extensions": pkg.document.extensions,
            "signatures": pkg.document.signatures,
            "versions": pkg.document.versions,
            "can_undo": self.editor.can_undo() if self.editor else False,
            "can_redo": self.editor.can_redo() if self.editor else False,
            "undo_label": self.editor.undo_label() if self.editor else "",
            "redo_label": self.editor.redo_label() if self.editor else "",
            "dirty": self.editor.dirty if self.editor else False,
        }

    # ---------------------------------------------------------- 权限（表8）
    def permissions(self) -> dict:
        pkg = self.require()
        return (pkg.document.permissions if pkg.documents else {}) or {}

    def check_permission(self, key: str, what: str) -> None:
        """表8：Edit/Export/Printable 这些声明不是建议，是文件自己的限制。"""
        perm = self.permissions()
        # 表8 ValidPeriod：过了允许访问的期限，同样要拦（只读浏览不受影响）
        state = extras.validity_status(perm)
        if state == "expired":
            raise EditError("文档已过访问有效期（表8 ValidPeriod 结束日期已到），不能%s" % what)
        if state == "not-started":
            raise EditError("文档访问有效期尚未开始（表8 ValidPeriod 开始日期未到），不能%s" % what)
        if perm.get(key, True) is False:
            raise EditError("文档权限声明禁止%s（表8 %s=false）" % (what, key))

    def find_attachment(self, att_id: str):
        pkg = self.require()
        for att in pkg.document.attachments:
            if att["id"] == att_id:
                return att
        return None

    def scene(self, index: int, with_objects: bool = True) -> dict:
        pkg = self.require()
        pages = pkg.all_pages()
        if not (0 <= index < len(pages)):
            raise EditError("页码越界: %d" % index)
        page = pages[index]
        sc = build_page_scene(page)
        out = sc.to_dict()
        out["index"] = index
        out["outline"] = page_outline(page)
        out["objects"] = selection_boxes(page, page.objects()) if with_objects else []
        out["page_count"] = len(pages)
        out["neighbours"] = [
            {"index": i, "id": pages[i].id,
             "w": round(pages[i].size().w, 2), "h": round(pages[i].size().h, 2)}
            for i in range(len(pages))
        ]
        self.page_index = index
        return out

    # ---------------------------------------------------------- 编辑
    def edit(self, req: dict) -> dict:
        with self.lock:
            pkg = self.require()
            ed = self.editor
            op = req.get("op")
            if op in ("save", "save_as"):
                # 表8：Edit=false 的文件不许改（撤销/查看不受限制）
                self.check_permission("Edit", "保存修改")
            pages = pkg.all_pages()

            def page_by(idx):
                i = int(idx)
                if not (0 <= i < len(pages)):
                    raise EditError("页码越界: %d" % i)
                return pages[i]

            def objs_of(pi, ids):
                page = page_by(pi)
                want = set(str(x) for x in ids)
                got = [o for o in page.objects() if o.id in want]
                if not got:
                    raise EditError("找不到对象: %s" % ",".join(want))
                return page, got

            if op == "set_text":
                page, got = objs_of(req["page"], [req["id"]])
                o = got[0]
                if not isinstance(o, model.TextObject):
                    raise EditError("对象 #%s 不是文本" % o.id)
                ed.set_text(o, str(req.get("text") or ""))
            elif op == "set_text_style":
                page, got = objs_of(req["page"], [req["id"]])
                o = got[0]
                color = req.get("color")
                rgba = _parse_hex(color) if color else None
                ed.set_text_style(
                    o,
                    size_mm=req.get("size"),
                    bold=req.get("bold"),
                    italic=req.get("italic"),
                    color=rgba,
                    font_id=req.get("font_id"),
                    spacing=req.get("spacing"),
                )
            elif op == "move":
                page, got = objs_of(req["page"], req.get("ids") or [])
                ed.move(got, float(req.get("dx") or 0), float(req.get("dy") or 0))
            elif op == "scale":
                page, got = objs_of(req["page"], req.get("ids") or [])
                ed.scale(got, float(req.get("sx") or 1), float(req.get("sy") or 1),
                         float(req.get("ax") or 0), float(req.get("ay") or 0))
            elif op == "rotate":
                page, got = objs_of(req["page"], req.get("ids") or [])
                ed.rotate(got, float(req.get("deg") or 0),
                          float(req.get("ax") or 0), float(req.get("ay") or 0))
            elif op == "delete":
                page, got = objs_of(req["page"], req.get("ids") or [])
                ed.delete_objects(got)
            elif op == "duplicate":
                page, got = objs_of(req["page"], req.get("ids") or [])
                ed.duplicate_objects(got, float(req.get("dx") or 5), float(req.get("dy") or 5))
            elif op == "set_fill":
                page, got = objs_of(req["page"], req.get("ids") or [])
                rgba = _parse_hex(req["color"])
                if rgba is None:
                    raise EditError("颜色格式不对")
                ed.set_fill(got, rgba)
            elif op == "set_stroke":
                page, got = objs_of(req["page"], req.get("ids") or [])
                rgba = _parse_hex(req["color"]) if req.get("color") else (0, 0, 0, 1)
                ed.set_stroke(got, rgba, req.get("width"))
            elif op == "set_alpha":
                page, got = objs_of(req["page"], req.get("ids") or [])
                ed.set_alpha(got, float(req.get("alpha") or 1))
            elif op == "add_text":
                page = page_by(req.get("page", 0))
                color = _parse_hex(req.get("color") or "#000000") or (0, 0, 0, 1)
                ed.add_text(page, float(req["x"]), float(req["y"]),
                            str(req.get("text") or "新文本"),
                            size_mm=float(req.get("size") or 5.0),
                            font_id=req.get("font_id"),
                            color=color, bold=bool(req.get("bold")))
            elif op == "add_image":
                page = page_by(req.get("page", 0))
                raw = base64.b64decode(req["data_b64"])
                ed.add_image(page, raw, req.get("name") or "image.png",
                             float(req["x"]), float(req["y"]),
                             float(req["w"]), float(req["h"]))
            elif op == "add_rect":
                page = page_by(req.get("page", 0))
                x, y, w, h = (float(req["x"]), float(req["y"]),
                              float(req["w"]), float(req["h"]))
                # 绝对页面坐标，一次调用落位（不额外 move，撤销只需一步）
                d = "M %s %s L %s %s L %s %s L %s %s Z" % (
                    geo._fmt(x), geo._fmt(y), geo._fmt(x + w), geo._fmt(y),
                    geo._fmt(x + w), geo._fmt(y + h), geo._fmt(x), geo._fmt(y + h))
                ed.add_path(page, [d], stroke=_parse_hex(req.get("color") or "#000000"),
                            fill=_parse_hex(req["fill"]) if req.get("fill") else None,
                            width_mm=float(req.get("width") or 0.4))
            elif op == "replace_all":
                n = ed.replace_all(str(req.get("find") or ""), str(req.get("repl") or ""))
                return {"ok": True, "replaced": n, "doc": self.describe()}
            elif op == "page_insert":
                ed.insert_page(int(req.get("index", len(pages))),
                               geo.Rect(0, 0, *(req.get("size") or
                                                [pages[0].size().w, pages[0].size().h])))
            elif op == "page_delete":
                ed.delete_page(int(req["index"]))
            elif op == "page_move":
                ed.move_page(int(req["src"]), int(req["dst"]))
            elif op == "page_rotate":
                page = page_by(req["page"])
                ed.set_page_rotation(page, int(req.get("deg", 0)))
            elif op == "page_size":
                page = page_by(req["page"])
                ed.set_page_size(page, float(req["w"]), float(req["h"]))
            elif op == "undo":
                label = ed.undo()
                return {"ok": True, "label": label, "doc": self.describe()}
            elif op == "redo":
                label = ed.redo()
                return {"ok": True, "label": label, "doc": self.describe()}
            elif op == "save":
                path = req.get("path") or pkg.path
                ed.save(path)
                return {"ok": True, "path": path, "doc": self.describe()}
            elif op == "save_as":
                target = req.get("path")
                if not target:
                    raise EditError("缺少目标路径")
                ed.save(target)
                return {"ok": True, "path": target, "doc": self.describe()}
            else:
                raise EditError("不认识的操作: %s" % op)

            return {"ok": True, "doc": self.describe(),
                    "page": self.scene(int(req.get("page", 0))
                                       if isinstance(req.get("page"), (int, float)) else 0)}

    # ---------------------------------------------------------- 导出
    def export(self, req: dict) -> dict:
        with self.lock:
            pkg = self.require()
            self.check_permission("Export", "导出")
            fmt = (req.get("format") or "pdf").lower()
            pages = req.get("pages")
            total = pkg.page_count()
            idx = [int(i) for i in pages] if pages else list(range(total))
            idx = [i for i in idx if 0 <= i < total]
            dpi = int(req.get("dpi") or 200)
            outdir = self.tmpdir
            if fmt == "pdf":
                path = os.path.join(outdir, "export.pdf")
                convert.export_pdf(pkg, path, dpi=dpi, pages=idx)
                return {"ok": True, "file": path, "name": "export.pdf",
                        "mime": "application/pdf", "size": os.path.getsize(path)}
            if fmt == "txt":
                path = os.path.join(outdir, "export.txt")
                convert.export_text(pkg, path)
                return {"ok": True, "file": path, "name": "export.txt",
                        "mime": "text/plain; charset=utf-8", "size": os.path.getsize(path)}
            if fmt == "svg":
                if len(idx) != 1:
                    raise EditError("SVG 一次导出一页，请指定单页")
                path = os.path.join(outdir, "page_%d.svg" % (idx[0] + 1))
                convert.export_svg(pkg.all_pages()[idx[0]], path)
                return {"ok": True, "file": path, "name": os.path.basename(path),
                        "mime": "image/svg+xml", "size": os.path.getsize(path)}
            if fmt in ("png", "jpeg", "jpg", "tiff"):
                if len(idx) != 1:
                    raise EditError("位图一次导出一页，请指定单页")
                from ofdlite.render_soft import render_page
                img, warn = render_page(pkg.all_pages()[idx[0]], dpi=dpi)
                ext = {"jpeg": "jpg", "jpg": "jpg"}.get(fmt, fmt)
                path = os.path.join(outdir, "page_%d.%s" % (idx[0] + 1, ext))
                img.save(path)
                mime = {"png": "image/png", "jpg": "image/jpeg",
                        "tiff": "image/tiff"}.get(ext, "application/octet-stream")
                return {"ok": True, "file": path, "name": os.path.basename(path),
                        "mime": mime, "size": os.path.getsize(path), "warnings": warn}
            raise EditError("不支持的导出格式: %s" % fmt)

    # ---------------------------------------------------------- 打印
    def print_job(self, req: dict) -> dict:
        with self.lock:
            from ofdlite import print_linux
            pkg = self.require()
            self.check_permission("Printable", "打印")
            # 表8：Copies 为 0 不许打印，为负或未设置＝份数不受限
            copies_req = int(req.get("copies") or 1)
            limit = extras.printable_count(self.permissions())
            if limit == 0:
                raise EditError("文档权限声明禁止打印（表8 Printable=false 或 Copies=0）")
            if limit is not None and copies_req > limit:
                raise EditError("文档权限声明限定最多打印 %d 份（表8 Copies）" % limit)
            pages = req.get("pages")
            total = pkg.page_count()
            idx = [int(i) for i in pages] if pages else list(range(total))
            idx = [i for i in idx if 0 <= i < total]
            if not idx:
                raise EditError("没有选中页面")
            res = print_linux.print_document(
                pkg,
                printer=req.get("printer") or print_linux.default_printer(),
                pages=idx,
                copies=copies_req,
                n_up=int(req.get("n_up") or 1),
                paper=req.get("paper") or "A4",
                duplex=bool(req.get("duplex")),
                booklet=bool(req.get("booklet")),
                dpi=int(req.get("dpi") or 200),
            )
            return res


SESSION = Session()


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "OFDLite/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------- 路由
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)
            if path == "/" or path == "/index.html":
                return self._serve_file(os.path.join(STATIC_DIR, "index.html"))
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                return self._serve_static(rel)
            if path == "/api/meta":
                return self._json(self._meta())
            if path == "/api/doc":
                with SESSION.lock:
                    return self._json({"ok": True, "doc": SESSION.describe()})
            if path.startswith("/api/page/"):
                idx = int(path.rsplit("/", 1)[1].split(".")[0])
                with SESSION.lock:
                    return self._json({"ok": True, "page": SESSION.scene(idx)})
            if path.startswith("/api/thumb/"):
                idx = int(path.rsplit("/", 1)[1].split(".")[0])
                dpi = int(query.get("dpi", ["40"])[0])
                return self._thumb(idx, dpi)
            if path.startswith("/api/render/"):
                idx = int(path.rsplit("/", 1)[1].split(".")[0])
                dpi = int(query.get("dpi", ["150"])[0])
                return self._render(idx, dpi)
            if path == "/api/printers":
                from ofdlite import print_linux
                return self._json({"ok": True, "printers": print_linux.list_printers(),
                                   "default": print_linux.default_printer(),
                                   "available": print_linux.cups_available()})
            if path == "/api/download":
                target = query.get("file", [""])[0]
                return self._serve_generated(target)
            if path.startswith("/api/attachment/"):
                # 第20章：附件下载
                return self._serve_attachment(path.split("/api/attachment/", 1)[1])
            return self._json({"ok": False, "error": "未知路径 %s" % path}, 404)
        except Exception as exc:
            return self._error(exc)

    def do_POST(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            body = self._read_json()
            if path == "/api/open":
                target = body.get("path")
                if body.get("data_b64"):
                    data = base64.b64decode(body["data_b64"])
                    doc = SESSION.open_bytes(data, body.get("name") or "upload.ofd")
                elif target:
                    doc = SESSION.open_path(os.path.expanduser(target))
                else:
                    raise EditError("缺少 path 或 data_b64")
                return self._json({"ok": True, "doc": doc,
                                   "page": SESSION.scene(0)})
            if path == "/api/edit":
                return self._json(SESSION.edit(body))
            if path == "/api/export":
                info = SESSION.export(body)
                return self._json({"ok": True, "file": info})
            if path == "/api/print":
                res = SESSION.print_job(body)
                return self._json({"ok": bool(res.get("ok")), "result": res})
            if path == "/api/page-set":
                with SESSION.lock:
                    SESSION.page_index = int(body.get("index") or 0)
                    return self._json({"ok": True})
            return self._json({"ok": False, "error": "未知接口"}, 404)
        except Exception as exc:
            return self._error(exc)

    # ---------------------------------------------------------- 输出
    def _meta(self):
        from ofdlite import print_linux
        return {
            "ok": True,
            "version": __import__("ofdlite").__version__,
    "build_id": __import__("ofdlite").build_id(),
    "signature_warning": "修改并另存后，原数字签名失效；改后的文件仅用于版式调整，不得用于报销、入账、归档。",
            "fonts": font_manager.describe(),
            "cups": print_linux.cups_available(),
            "printers": print_linux.list_printers(),
            "default_printer": print_linux.default_printer(),
            "python": sys.version.split()[0],
        }

    def _thumb(self, idx, dpi):
        from ofdlite.render_soft import render_page
        with SESSION.lock:
            pkg = SESSION.require()
            pages = pkg.all_pages()
            if not (0 <= idx < len(pages)):
                return self._json({"ok": False, "error": "页码越界"}, 400)
            img, _w = render_page(pages[idx], dpi=max(20, min(120, dpi)))
        import io
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return self._png(buf.getvalue())

    def _render(self, idx, dpi):
        from ofdlite.render_soft import render_page
        with SESSION.lock:
            pkg = SESSION.require()
            pages = pkg.all_pages()
            if not (0 <= idx < len(pages)):
                return self._json({"ok": False, "error": "页码越界"}, 400)
            img, _w = render_page(pages[idx], dpi=max(36, min(600, dpi)))
        import io
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return self._png(buf.getvalue())

    def _png(self, data: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, rel: str):
        rel = posixpath.normpath(rel).replace("\\", "/")
        if rel.startswith("..") or rel.startswith("/"):
            return self._json({"ok": False, "error": "非法路径"}, 400)
        target = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(target):
            return self._json({"ok": False, "error": "文件不存在: %s" % rel}, 404)
        return self._serve_file(target)

    def _serve_file(self, target: str):
        if not os.path.isfile(target):
            return self._json({"ok": False, "error": "文件不存在"}, 404)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_attachment(self, att_id: str):
        """第20章：把附件内容按原名吐出来（只读包内字节，不落盘）。"""
        with SESSION.lock:
            att = SESSION.find_attachment(att_id)
            if att is None:
                return self._json({"ok": False, "error": "没有这个附件: %s" % att_id}, 404)
            entry = att.get("entry") or ""
            pkg = SESSION.require()
            if not entry or not pkg.has(entry):
                return self._json({"ok": False,
                                   "error": "附件条目缺失: %s" % (entry or "(空)")}, 404)
            data = pkg.read(entry)
        name = att.get("name") or os.path.basename(entry) or ("attachment-%s" % att_id)
        ctype = (mimetypes.guess_type(name)[0]
                 or mimetypes.guess_type(entry)[0]
                 or "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % urllib.parse.quote(name))
        self.end_headers()
        self.wfile.write(data)

    def _serve_generated(self, target: str):
        """只允许下载服务自己生成的临时产物。"""
        if not target:
            return self._json({"ok": False, "error": "缺少 file"}, 400)
        real = os.path.realpath(target)
        if not real.startswith(os.path.realpath(SESSION.tmpdir)):
            return self._json({"ok": False, "error": "只能下载本次导出的文件"}, 403)
        if not os.path.isfile(real):
            return self._json({"ok": False, "error": "文件不存在"}, 404)
        name = os.path.basename(real)
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        with open(real, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % urllib.parse.quote(name))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc):
        code = 400 if isinstance(exc, (EditError, OfdError, ValueError, KeyError)) else 500
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        sys.stderr.write("[api] %s\n" % detail)
        if code == 500:
            traceback.print_exc()
        return self._json({"ok": False, "error": detail}, code)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_UPLOAD:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def parse_hex(value):
    return _parse_hex(value)


def _parse_hex(text):
    if not text:
        return None
    from ofdlite import colors as colorlib
    return colorlib.parse_color(text)


def main(argv=None):
    ap = argparse.ArgumentParser(description="OFD 网页端")
    ap.add_argument("--port", type=int, default=8760)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", dest="open_file", help="启动时打开的 OFD")
    args = ap.parse_args(argv)

    if args.open_file:
        SESSION.open_path(os.path.abspath(args.open_file))
        print("已打开:", args.open_file)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    url = "http://%s:%d" % (args.host, args.port)
    print("OFD 网页端: %s" % url)
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

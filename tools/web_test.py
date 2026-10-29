# -*- coding: utf-8 -*-
"""网页端接口自测。

    python tools/web_test.py [--port 8761]

启动一个临时 server 子进程，逐项打接口，最后关掉。退出码 0 = 全过。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

PASS, FAIL = [], []


def check(name):
    def deco(fn):
        try:
            fn()
            PASS.append(name)
            print("  ok   %s" % name)
        except Exception as exc:
            FAIL.append((name, exc))
            print("  FAIL %s: %s" % (name, exc))
        return fn
    return deco


class Client(object):
    def __init__(self, base):
        self.base = base

    def get(self, path):
        req = urllib.request.Request(self.base + path, method="GET")
        with urllib.request.urlopen(req, timeout=60) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read()
            if "json" in ctype:
                return json.loads(data.decode("utf-8"))
            return data

    def post(self, path, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "replace")
            try:
                return json.loads(payload)
            except Exception:
                raise RuntimeError("HTTP %s: %s" % (exc.code, payload[:200]))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8761)
    ap.add_argument("--keep", action="store_true", help="跑完不关闭服务")
    args = ap.parse_args(argv)

    sample = os.path.join(ROOT, "out", "sample.ofd")
    if not os.path.exists(sample):
        import make_sample_ofd
        os.makedirs(os.path.dirname(sample), exist_ok=True)
        make_sample_ofd.build(sample)

    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "web", "server.py"),
         "--port", str(args.port), "--open", sample],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % args.port
    c = Client(base)

    # 等端口起来
    for _ in range(100):
        try:
            c.get("/api/meta")
            break
        except Exception:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                print("服务启动失败：\n" + out)
                return 1
            time.sleep(0.15)

    state = {}

    @check("GET /api/meta 返回字体与 CUPS 状态")
    def _():
        m = c.get("/api/meta")
        if not m.get("ok"):
            raise AssertionError(m)
        if "fonts" not in m or "families" not in m["fonts"]:
            raise AssertionError(m.get("fonts"))
        state["meta"] = m

    @check("GET / 返回页面 HTML")
    def _():
        html = c.get("/").decode("utf-8", "replace")
        if "OFD" not in html or "canvas" not in html:
            raise AssertionError(html[:120])

    @check("静态资源可访问")
    def _():
        for p in ("/static/app.js", "/static/canvas.js", "/static/style.css"):
            data = c.get(p)
            if len(data) < 500:
                raise AssertionError("%s 太小 %d" % (p, len(data)))

    @check("GET /api/doc 有页信息")
    def _():
        d = c.get("/api/doc")
        if not d["ok"] or len(d["doc"]["pages"]) != 2:
            raise AssertionError(d)
        state["doc"] = d["doc"]

    @check("GET /api/page/0 场景含 ops/objects")
    def _():
        d = c.get("/api/page/0")
        p = d["page"]
        if not p["ops"]:
            raise AssertionError("ops 空")
        if not p["objects"]:
            raise AssertionError("objects 空")
        kinds = set(op["t"] for op in p["ops"])
        for need in ("text", "path", "image"):
            if need not in kinds:
                raise AssertionError(kinds)
        if not p.get("outline"):
            raise AssertionError("缺 outline")
        state["page"] = p

    @check("缩略图与位图接口返回 PNG")
    def _():
        for path in ("/api/thumb/0?dpi=30", "/api/render/0?dpi=80"):
            data = c.get(path)
            if not data.startswith(b"\x89PNG"):
                raise AssertionError("%s 不是 PNG" % path)

    @check("编辑：改文本并回读")
    def _():
        obj = None
        for o in state["page"]["objects"]:
            if o["type"] == "文本":
                obj = o
                break
        if not obj:
            raise AssertionError("页面上没有文本对象")
        state["obj"] = obj
        r = c.post("/api/edit", {"op": "set_text", "page": 0,
                                 "id": obj["id"], "text": "网页端改的标题"})
        if not r.get("ok"):
            raise AssertionError(r)
        p = c.get("/api/page/0")["page"]
        found = [o for o in p["objects"] if o["id"] == obj["id"]]
        if not found or found[0]["text"] != "网页端改的标题":
            raise AssertionError(found[:1])

    @check("编辑：文字样式（字号+颜色+加粗）")
    def _():
        obj = state["obj"]
        r = c.post("/api/edit", {"op": "set_text_style", "page": 0, "id": obj["id"],
                                 "size": 12, "color": "#C81E1E", "bold": True})
        if not r.get("ok"):
            raise AssertionError(r)
        p = c.get("/api/page/0")["page"]
        o = [x for x in p["objects"] if x["id"] == obj["id"]][0]
        if abs(o["size"] - 12) > 0.01:
            raise AssertionError(o["size"])
        if not o["bold"]:
            raise AssertionError("bold 没生效")

    @check("编辑：移动对象")
    def _():
        obj = state["obj"]
        r = c.post("/api/edit", {"op": "move", "page": 0, "ids": [obj["id"]],
                                 "dx": 7, "dy": 3})
        if not r.get("ok"):
            raise AssertionError(r)
        p = c.get("/api/page/0")["page"]
        o = [x for x in p["objects"] if x["id"] == obj["id"]][0]
        x0 = obj["corners"][0][0]
        x1 = o["corners"][0][0]
        if abs((x1 - x0) - 7) > 0.2:
            raise AssertionError((x0, x1))

    @check("编辑：插入文字")
    def _():
        n0 = len(c.get("/api/page/0")["page"]["objects"])
        r = c.post("/api/edit", {"op": "add_text", "page": 0, "x": 20, "y": 280,
                                 "text": "网页端插入", "size": 6})
        if not r.get("ok"):
            raise AssertionError(r)
        n1 = len(c.get("/api/page/0")["page"]["objects"])
        if n1 != n0 + 1:
            raise AssertionError((n0, n1))

    @check("编辑：插入方框")
    def _():
        n0 = len(c.get("/api/page/0")["page"]["objects"])
        r = c.post("/api/edit", {"op": "add_rect", "page": 0, "x": 30, "y": 200,
                                 "w": 60, "h": 30, "color": "#0072C6"})
        if not r.get("ok"):
            raise AssertionError(r)
        p = c.get("/api/page/0")["page"]
        if len(p["objects"]) != n0 + 1:
            raise AssertionError(len(p["objects"]))

    @check("编辑：插入图片（base64）")
    def _():
        png = _tiny_png()
        b64 = base64.b64encode(png).decode("ascii")
        n0 = len(c.get("/api/page/0")["page"]["objects"])
        r = c.post("/api/edit", {"op": "add_image", "page": 0, "x": 20, "y": 240,
                                 "w": 30, "h": 20, "data_b64": b64, "name": "web.png"})
        if not r.get("ok"):
            raise AssertionError(r)
        p = c.get("/api/page/0")["page"]
        imgs = [o for o in p["objects"] if o["type"] == "图片"]
        if len(imgs) < 2:
            raise AssertionError(imgs)

    @check("撤销 / 重做")
    def _():
        d1 = c.post("/api/edit", {"op": "undo"})
        if not d1.get("ok"):
            raise AssertionError(d1)
        c.post("/api/edit", {"op": "redo"})
        if not c.post("/api/edit", {"op": "undo"}).get("ok"):
            raise AssertionError("第二次撤销失败")

    @check("查找替换")
    def _():
        r = c.post("/api/edit", {"op": "replace_all", "find": "文档", "repl": "文件"})
        if not r.get("ok"):
            raise AssertionError(r)
        if "replaced" not in r:
            raise AssertionError(r)
        c.post("/api/edit", {"op": "undo"})

    @check("插页 / 删页 / 旋转")
    def _():
        n0 = len(c.get("/api/doc")["doc"]["pages"])
        c.post("/api/edit", {"op": "page_insert", "index": 1})
        n1 = len(c.get("/api/doc")["doc"]["pages"])
        if n1 != n0 + 1:
            raise AssertionError((n0, n1))
        c.post("/api/edit", {"op": "page_delete", "index": 1})
        if len(c.get("/api/doc")["doc"]["pages"]) != n0:
            raise AssertionError("删页失败")
        c.post("/api/edit", {"op": "page_rotate", "page": 0, "deg": 90})
        if c.get("/api/doc")["doc"]["pages"][0]["rotation"] != 90:
            raise AssertionError("旋转没生效")
        c.post("/api/edit", {"op": "undo"})

    @check("导出 PDF 并下载")
    def _():
        r = c.post("/api/export", {"format": "pdf", "dpi": 96})
        f = r["file"]
        if f["size"] < 3000:
            raise AssertionError(f)
        data = c.get("/api/download?file=" + urllib.parse.quote(f["file"]))
        if not data.startswith(b"%PDF"):
            raise AssertionError(data[:8])

    @check("导出 PNG / SVG / TXT")
    def _():
        r1 = c.post("/api/export", {"format": "png", "dpi": 72, "pages": [0]})
        if c.get("/api/download?file=" + urllib.parse.quote(r1["file"]["file"]))[:4] != b"\x89PNG":
            raise AssertionError("PNG 不对")
        r2 = c.post("/api/export", {"format": "svg", "pages": [0]})
        svg = c.get("/api/download?file=" + urllib.parse.quote(r2["file"]["file"]))
        if b"<svg" not in svg:
            raise AssertionError("SVG 不对")
        r3 = c.post("/api/export", {"format": "txt"})
        txt = c.get("/api/download?file=" + urllib.parse.quote(r3["file"]["file"]))
        if "第 1 页".encode("utf-8") not in txt:
            raise AssertionError(txt[:60])

    @check("保存到文件并重新打开")
    def _():
        target = os.path.join(ROOT, "out", "web_saved.ofd")
        r = c.post("/api/edit", {"op": "save_as", "path": target})
        if not r.get("ok"):
            raise AssertionError(r)
        if not os.path.exists(target) or os.path.getsize(target) < 1000:
            raise AssertionError("文件没写出来")
        r2 = c.post("/api/open", {"path": target})
        if not r2.get("ok") or len(r2["doc"]["pages"]) != 2:
            raise AssertionError(r2)

    @check("打印机列表接口")
    def _():
        d = c.get("/api/printers")
        if "printers" not in d or "available" not in d:
            raise AssertionError(d)

    @check("错误路径返回 JSON 错误")
    def _():
        try:
            c.get("/api/nope")
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            if body.get("ok") is not False:
                raise AssertionError(body)
            return
        raise AssertionError("应该 404")

    @check("下载接口越权访问被拒")
    def _():
        try:
            c.get("/api/download?file=" + urllib.parse.quote("/etc/passwd"))
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 404):
                raise AssertionError(exc.code)
            return
        raise AssertionError("应该被拒")

    # ---- 第 7.5 / 16 / 17 / 18 / 19 / 20 章：附属结构 + 权限生效
    extras_path = os.path.join(ROOT, "out", "extras_test.ofd")
    if not os.path.exists(extras_path):
        import make_extras_ofd
        os.makedirs(os.path.dirname(extras_path), exist_ok=True)
        make_extras_ofd.build(extras_path)

    @check("打开附属结构样本：meta 里带齐权限/书签/附件/签名/版本")
    def _():
        r = c.post("/api/open", {"path": extras_path})
        if not r.get("ok"):
            raise AssertionError(r)
        doc = r["doc"]
        if doc.get("permissions", {}).get("Export") is not False:
            raise AssertionError(doc.get("permissions"))
        if doc.get("permissions", {}).get("Copies") != 2:
            raise AssertionError(doc.get("permissions"))
        if len(doc.get("bookmarks") or []) != 2:
            raise AssertionError(doc.get("bookmarks"))
        if len(doc.get("attachments") or []) != 2:
            raise AssertionError(doc.get("attachments"))
        if not (doc.get("signatures") or {}).get("items"):
            raise AssertionError(doc.get("signatures"))
        if len((doc.get("versions") or {}).get("items") or []) != 2:
            raise AssertionError(doc.get("versions"))
        if not doc.get("custom_tags") or not doc.get("extensions"):
            raise AssertionError((doc.get("custom_tags"), doc.get("extensions")))
        if (doc.get("vpreferences") or {}).get("Zoom") != 120:
            raise AssertionError(doc.get("vpreferences"))

    @check("表8：Export=false 时导出被拒")
    def _():
        r = c.post("/api/export", {"format": "pdf", "pages": [0]})
        if r.get("ok") is not False or "Export" not in str(r.get("error")):
            raise AssertionError(r)

    @check("表8：Printable=false 时打印被拒")
    def _():
        r = c.post("/api/print", {"pages": [0]})
        if r.get("ok") is not False or "打印" not in str(r.get("error")):
            raise AssertionError(r)

    @check("第20章：附件可按 ID 取回内容")
    def _():
        data = c.get("/api/attachment/A1")
        if "A1" not in data.decode("utf-8", "replace") and len(data) < 4:
            raise AssertionError(data[:40])

    if not args.keep:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print()
    print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        for name, exc in FAIL:
            print("  -", name, ":", exc)
        if proc.poll() is None and proc.stdout:
            try:
                proc.terminate()
            except Exception:
                pass
        return 1
    return 0


def _tiny_png():
    import io
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (60, 40), (30, 120, 200)).save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        import struct
        import zlib

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data +
                    struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        raw = b"".join(b"\x00" + b"\x1e\x78\xc8" * 60 for _ in range(40))
        return (b"\x89PNG\r\n\x1a\n" +
                chunk(b"IHDR", struct.pack(">IIBBBBB", 60, 40, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())

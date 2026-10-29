# -*- coding: utf-8 -*-
"""自测：覆盖解析、渲染、编辑、撤销、保存、导出、打印命令生成。

    python tools/selftest.py            # 全量
    python tools/selftest.py -v         # 带细节
退出码 0 = 全过。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import traceback
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofdlite import OFDPackage, build_page_scene, geometry as geo, model
from ofdlite.convert import (export_pdf, export_svg, export_text,
                             page_to_svg, structure_report)
from ofdlite.edits import Editor, EditError
from ofdlite.render_pil import render_page, PilRenderer

PASS = []
FAIL = []
VERBOSE = "-v" in sys.argv


def check(name):
    def deco(fn):
        try:
            fn()
            PASS.append(name)
            print("  ok   %s" % name)
        except Exception as exc:
            FAIL.append((name, exc))
            print("  FAIL %s: %s" % (name, exc))
            if VERBOSE:
                traceback.print_exc()
        return fn
    return deco


def sample(path: str) -> str:
    sys.path.insert(0, HERE)
    import make_sample_ofd
    return make_sample_ofd.build(path, pages=2, with_image=True)


def approx(a, b, tol=0.01):
    if abs(a - b) > tol:
        raise AssertionError("%r != %r (tol %r)" % (a, b, tol))


def main():
    tmpdir = tempfile.mkdtemp(prefix="ofdlite-")
    sample_path = os.path.join(tmpdir, "sample.ofd")
    sample(sample_path)
    print("样本:", sample_path)

    pkg = OFDPackage.open(sample_path)

    @check("打开：页数与文档数")
    def _():
        if pkg.page_count() != 2:
            raise AssertionError("页数 %d" % pkg.page_count())
        if len(pkg.documents) != 1:
            raise AssertionError("文档数 %d" % len(pkg.documents))

    @check("页面尺寸 210x297")
    def _():
        p = pkg.all_pages()[0]
        approx(p.size().w, 210)
        approx(p.size().h, 297)

    @check("对象数与类型分布")
    def _():
        p = pkg.all_pages()[0]
        kinds = {}
        for o in p.objects():
            kinds[o.type_name] = kinds.get(o.type_name, 0) + 1
        if kinds.get("文本", 0) < 5 or kinds.get("路径", 0) < 3:
            raise AssertionError(kinds)
        if kinds.get("图片", 0) != 1:
            raise AssertionError("图片数 %r" % kinds)

    @check("文本提取")
    def _():
        p = pkg.all_pages()[0]
        texts = [o.text() for o in p.objects() if isinstance(o, model.TextObject)]
        joined = "".join(texts)
        if "银河麒麟" not in joined:
            raise AssertionError(joined[:60])

    @check("场景指令包含 text/path/image")
    def _():
        sc = build_page_scene(pkg.all_pages()[0])
        ts = set(op["t"] for op in sc.ops)
        for need in ("text", "path", "image"):
            if need not in ts:
                raise AssertionError(ts)
        if not sc.resources:
            raise AssertionError("没有图片资源")

    @check("渲染 PNG 非空白")
    def _():
        img, warn = render_page(pkg.all_pages()[0], dpi=96)
        if img.size != (int(round(210 * 96 / 25.4)), int(round(297 * 96 / 25.4))):
            raise AssertionError(img.size)
        # 抽样：不是纯白
        px = img.convert("L").load()
        dark = 0
        for yy in range(0, img.size[1], 7):
            for xx in range(0, img.size[0], 7):
                if px[xx, yy] < 200:
                    dark += 1
        if dark < 50:
            raise AssertionError("画面几乎是空白 dark=%d" % dark)

    @check("命中测试")
    def _():
        p = pkg.all_pages()[0]
        hit = p.hit_test(50, 30)      # 标题文字区
        if hit is None or not isinstance(hit, model.TextObject):
            raise AssertionError(hit)
        if p.hit_test(5, 290) is not None:   # 空白角落
            raise AssertionError("空白处不该命中")

    editor = Editor(pkg)

    @check("改文本 → 撤销 → 重做")
    def _():
        p = pkg.all_pages()[0]
        obj = [o for o in p.objects() if isinstance(o, model.TextObject)][0]
        old = obj.text()
        editor.set_text(obj, "改过的标题")
        obj2 = [o for o in p.objects() if o.id == obj.id][0]
        if obj2.text() != "改过的标题":
            raise AssertionError(obj2.text())
        editor.undo()
        obj3 = [o for o in p.objects() if o.id == obj.id][0]
        if obj3.text() != old:
            raise AssertionError("undo 未还原: %r" % obj3.text())
        editor.redo()
        obj4 = [o for o in p.objects() if o.id == obj.id][0]
        if obj4.text() != "改过的标题":
            raise AssertionError("redo 未生效: %r" % obj4.text())

    @check("移动对象改变世界坐标")
    def _():
        p = pkg.all_pages()[0]
        obj = [o for o in p.objects() if isinstance(o, model.PathObject)][0]
        before = obj.world_bounds()
        editor.move([obj], 10, 5)
        obj2 = [o for o in p.objects() if o.id == obj.id][0]
        after = obj2.world_bounds()
        approx(after.x - before.x, 10, 0.05)
        approx(after.y - before.y, 5, 0.05)
        editor.undo()

    @check("插入文本对象")
    def _():
        p = pkg.all_pages()[0]
        n0 = len(p.objects())
        editor.add_text(p, 20, 200, "新插入的一行文字", size_mm=6)
        objs = p.objects()
        if len(objs) != n0 + 1:
            raise AssertionError("对象数 %d -> %d" % (n0, len(objs)))
        found = [o for o in objs if isinstance(o, model.TextObject) and "新插入" in o.text()]
        if not found:
            raise AssertionError("找不到新文本")
        editor.undo()
        if len(p.objects()) != n0:
            raise AssertionError("撤销后对象数 %d" % len(p.objects()))

    @check("插入图片对象 + 资源落包")
    def _():
        p = pkg.all_pages()[0]
        png = _tiny_png()
        obj = editor.add_image(p, png, "hello.png", 20, 210, 30, 20)
        if obj is None or not isinstance(obj, model.ImageObject):
            raise AssertionError(obj)
        got = obj.image_bytes()
        if got is None:
            raise AssertionError("资源没找到")
        if got[1] != png:
            raise AssertionError("字节不一致")
        editor.undo()

    @check("插入路径对象")
    def _():
        p = pkg.all_pages()[0]
        n0 = len(p.objects())
        editor.add_path(p, ["M 30 250 L 90 250 L 90 270 Z"], stroke=(1, 0, 0, 1))
        if len(p.objects()) != n0 + 1:
            raise AssertionError("对象数没增加")
        editor.undo()

    @check("查找替换")
    def _():
        n = editor.replace_all("测试", "验证")
        if n < 1:
            raise AssertionError("替换次数 %d" % n)
        joined = "".join(o.text() for o in pkg.all_pages()[0].objects()
                         if isinstance(o, model.TextObject))
        if "验证" not in joined:
            raise AssertionError(joined[:80])
        editor.undo()

    @check("插入页 / 删除页")
    def _():
        n0 = pkg.page_count()
        editor.insert_page(1)
        if pkg.page_count() != n0 + 1:
            raise AssertionError("%d -> %d" % (n0, pkg.page_count()))
        editor.undo()
        if pkg.page_count() != n0:
            raise AssertionError("撤销后 %d" % pkg.page_count())

    @check("保存后重开：改动力保留，未改条目字节不变")
    def _():
        p = pkg.all_pages()[0]
        obj = [o for o in p.objects() if isinstance(o, model.TextObject)][0]
        editor.set_text(obj, "保存验证标题")
        out = os.path.join(tmpdir, "saved.ofd")
        editor.save(out)
        # 字节对比：PublicRes / DocumentRes / 图片 应完全一致
        base_zip = zipfile.ZipFile(sample_path)
        new_zip = zipfile.ZipFile(out)
        try:
            for name in ("Doc_0/PublicRes.xml", "Doc_0/DocumentRes.xml", "Doc_0/Res/logo.png"):
                if base_zip.read(name) != new_zip.read(name):
                    raise AssertionError("条目被无谓改写: %s" % name)
        finally:
            base_zip.close()
            new_zip.close()
        pkg2 = OFDPackage.open(out)
        texts = "".join(o.text() for o in pkg2.all_pages()[0].objects()
                        if isinstance(o, model.TextObject))
        if "保存验证标题" not in texts:
            raise AssertionError(texts[:80])

    @check("结构报告")
    def _():
        rep = structure_report(pkg)
        if rep["pages"] < 2:
            raise AssertionError(rep["pages"])
        if not rep["docs"][0]["pages"][0]["kinds"]:
            raise AssertionError("kinds 空")

    @check("导出 SVG")
    def _():
        svg = page_to_svg(pkg.all_pages()[0])
        if "<svg" not in svg or "</svg>" not in svg:
            raise AssertionError("SVG 不完整")
        if "银河麒麟" not in svg and "保存验证" not in svg:
            raise AssertionError("SVG 里没有文字")
        if "data:image/png;base64" not in svg:
            raise AssertionError("SVG 里没有内联图片")

    @check("导出 PDF")
    def _():
        out = os.path.join(tmpdir, "out.pdf")
        export_pdf(pkg, out, dpi=100)
        size = os.path.getsize(out)
        if size < 5000:
            raise AssertionError("PDF 太小 %d" % size)
        with open(out, "rb") as fh:
            head = fh.read(5)
        if head != b"%PDF-":
            raise AssertionError(head)

    @check("导出文本")
    def _():
        txt = export_text(pkg)
        if "第 1 页" not in txt:
            raise AssertionError(txt[:60])

    @check("打印命令生成（不真打）")
    def _():
        from ofdlite.print_linux import build_lp_command, plan_sheets
        cmd = build_lp_command(["/tmp/a.pdf"], printer="Test", copies=2,
                               media="A4", duplex="long-edge", fit=True)
        if "lp" not in cmd[0]:
            raise AssertionError(cmd)
        if cmd.count("-n") != 1 or "2" not in cmd:
            raise AssertionError(cmd)
        sheets = plan_sheets(5, n_up=2, duplex=True)
        # 双面 2 合 1：一张纸 4 页，5 页内容 → 2 张纸
        if len(sheets) != 2:
            raise AssertionError("双面 2 合 1 的 5 页应是 2 张纸，得到 %d" % len(sheets))
        if len(sheets[0]) != 4:
            raise AssertionError("每张纸应有 4 个页位，得到 %d" % len(sheets[0]))

    print()
    print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print("失败项：")
        for name, exc in FAIL:
            print("  -", name, ":", exc)
        return 1
    return 0


def _tiny_png() -> bytes:
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), (200, 30, 30)).save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        import struct
        import zlib

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data +
                    struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        raw = b"".join(b"\x00" + b"\xc8\x1e\x1e" * 40 for _ in range(30))
        return (b"\x89PNG\r\n\x1a\n" +
                chunk(b"IHDR", struct.pack(">IIBBBBB", 40, 30, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    sys.exit(main())

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

    # ---------------------------------------------------------- 文档级附属结构
    extras_path = os.path.join(tmpdir, "extras.ofd")
    sys.path.insert(0, HERE)
    import make_extras_ofd
    make_extras_ofd.build(extras_path)
    epkg = OFDPackage.open(extras_path)
    edoc = epkg.document

    @check("表3/表8：DocType、权限声明（含 Print 子节点）")
    def _():
        if epkg.doc_type != "OFD" or epkg.ofd_version != "1.0":
            raise AssertionError((epkg.doc_type, epkg.ofd_version))
        perm = edoc.permissions
        if perm["Export"] is not False or perm["Printable"] is not False:
            raise AssertionError(perm)
        if perm["Copies"] != 2:
            raise AssertionError(perm)
        from ofdlite import extras
        if extras.printable_count(perm) != 0:
            raise AssertionError("Printable=false 时应为 0 份")

    @check("表9：视图首选项（子元素写法也要认）")
    def _():
        pref = edoc.vpreferences
        if pref["PageMode"] != "UseOutlines" or pref["PageLayout"] != "OnePage":
            raise AssertionError(pref)
        if pref["Zoom"] != 120:
            raise AssertionError(pref)

    @check("表10：书签与 Dest（页面/位置/缩放）")
    def _():
        bms = edoc.bookmarks
        if len(bms) != 2 or bms[0]["name"] != "封面":
            raise AssertionError(bms)
        if bms[0]["dest"]["page_id"] != "10" or bms[0]["dest"]["zoom"] != 100:
            raise AssertionError(bms[0])

    @check("表72/73：附件列表与包内条目解析")
    def _():
        atts = edoc.attachments
        if len(atts) != 2 or atts[0]["id"] != "A1":
            raise AssertionError(atts)
        if not epkg.has(atts[0]["entry"]):
            raise AssertionError(atts[0])
        if b"A1" not in epkg.read(atts[0]["entry"]):
            raise AssertionError("附件内容不对")

    @check("表63/64：自定义标引与扩展信息")
    def _():
        if not edoc.custom_tags or edoc.custom_tags[0]["namespace"].startswith("http") is False:
            raise AssertionError(edoc.custom_tags)
        if not edoc.extensions or edoc.extensions[0]["properties"][0]["name"] != "来源":
            raise AssertionError(edoc.extensions)

    @check("表66-69：签名列表与签名描述（只解析不验签）")
    def _():
        info = edoc.signatures
        if info.get("max_sign_id") != "s1":
            raise AssertionError(info)
        it = info["items"][0]
        if it["type"] != "Seal" or it["provider"] != "SampleSigner":
            raise AssertionError(it)
        if it["datetime"] != "2026-10-30T01:02:03" or it["method"] != "SM3withSM2":
            raise AssertionError(it)
        if len(it["references"]) != 2 or it["references"][0]["value"] != "AAAA":
            raise AssertionError(it["references"])
        if not it["stamp_annots"] or it["stamp_annots"][0]["page_ref"] != "10":
            raise AssertionError(it["stamp_annots"])
        if it["seal"] != "Seal.esl" or it["signed_value"] != "SignedValue.dat":
            raise AssertionError(it)

    @check("表70/71：版本列表（内嵌 OFD.xml）与版本描述")
    def _():
        vers = edoc.versions["items"]
        if len(vers) != 2 or vers[0]["name"] != "初版":
            raise AssertionError(vers)
        if vers[0]["doc_root"] != "/Doc_0/Document.xml":
            raise AssertionError(vers[0])
        if edoc.versions["current"] != "v1":
            raise AssertionError(edoc.versions)

    @check("表51-59：五类动作 + CT_Region（9.3 非紧缩描述）")
    def _():
        page = epkg.all_pages()[0]
        acts = []
        for o in page.objects():
            acts.extend(o.actions())
        kinds = set(a["kind"] for a in acts)
        for need in ("goto", "uri", "gotoa", "sound", "movie"):
            if need not in kinds:
                raise AssertionError(kinds)
        gotoa = [a for a in acts if a["kind"] == "gotoa"][0]
        if gotoa["attach_id"] != "A1":
            raise AssertionError(gotoa)
        # 模型层给的是对象空间（Boundary 左上角为原点，8.1.4）
        region = gotoa.get("region") or {}
        if region.get("bbox") != [0.0, 0.0, 60.0, 20.0]:
            raise AssertionError("模型层 region 应为对象空间: %r" % (region,))
        # scene 层必须抬到页面坐标（表51 的启动区域作用在页面上）
        from ofdlite.scene import build_page_scene
        op = [o for o in build_page_scene(page).ops if o.get("id") == "42"][0]
        scene_region = op["actions"][0].get("region") or {}
        if scene_region.get("bbox") != [20.0, 40.0, 60.0, 20.0]:
            raise AssertionError("scene 层 region 应为页面坐标: %r" % (scene_region,))
        goto = [a for a in acts if a["kind"] == "goto"][0]
        if goto["dest_detail"]["zoom"] != 200 or goto["dest_detail"]["top"] != 6:
            raise AssertionError(goto)

    @check("表44：FixedWidth 只作替代字型提示，不改排版步进")
    def _():
        from ofdlite.scene import build_page_scene
        page = epkg.all_pages()[0]
        fs = edoc.find_font("2")
        if fs is None or not fs.fixed_width:
            raise AssertionError("字体 2 应带 FixedWidth 标记")
        sc = build_page_scene(page)
        texts = [op for op in sc.ops if op.get("t") == "text" and op.get("id") == "41"]
        if not texts:
            raise AssertionError("没生成文字指令")
        # 指令上要带 fixed_width（给渲染端做替代字体偏好），
        # 但不能因此把字距改成 em——表44 原文写的是"用于匹配替代字型"
        if not any(op.get("fixed_width") for op in texts):
            raise AssertionError(texts[0])
        from ofdlite.scene import _advance
        if _advance("A", 10.0) >= 10.0:
            raise AssertionError("普通字符不应按 em 推进")

    @check("第13章：标准拼写的 CompositeGraphicUnit 能展开")
    def _():
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import std_gap_probe
        p = std_gap_probe.build_composite_sample(tmpdir)
        pk = OFDPackage.open(p)
        page = pk.all_pages()[0]
        objs = [o for o in page.objects() if type(o).__name__ == "CompositeObject"]
        if not objs or not objs[0].content_objects():
            raise AssertionError("复合对象没展开")

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

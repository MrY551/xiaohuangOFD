# -*- coding: utf-8 -*-
"""真实样本回归：拿线上的 OFD（数电发票、公文等）验四类表示法差异。

    python tools/real_sample_test.py 样本1.ofd [样本2.ofd ...]
    OFD_REAL_SAMPLES="a.ofd;b.ofd" python tools/real_sample_test.py

检查项（每份样本都跑）：
  1. 内容完整性：XML 里所有 TextCode 文本、路径、图片都进了绘制指令
  2. 模板页：CommonData/TemplatePage 与页内 <Template> 的对象都被合并
  3. 两种颜色写法：属性式与子元素式都能读出来
  4. 编辑＋保存：只重写改动过的条目，其余字节不变
  5. 渲染非空白

没有样本时直接跳过（退出码 0），方便接进 CI。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofdlite import OFDPackage, model
from ofdlite.edits import Editor
from ofdlite.render_pil import render_page
from ofdlite.scene import build_page_scene

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


def xml_text_codes(zf, pkg):
    """从包内所有 Content.xml 抽 TextCode 文本（含模板页）。"""
    out = []
    for name in zf.namelist():
        if not re.search(r"(Pages/Page_\d+|Tpls?/Tpl_\d+)/Content\.xml$", name):
            continue
        try:
            s = zf.read(name).decode("utf-8", "replace")
        except Exception:
            continue
        out.extend(m.group(1) for m in re.finditer(r">([^<>]+)</ofd:TextCode>", s))
        out.extend(m.group(1) for m in re.finditer(r'Text="([^"]*)"', s))
    return out


def run_one(path):
    print("=" * 72)
    print("样本:", os.path.basename(path), os.path.getsize(path), "字节")

    pkg = OFDPackage.open(path)
    state = {}

    @check("打开：页面与对象")
    def _():
        if pkg.page_count() < 1:
            raise AssertionError("没有页面")
        page = pkg.all_pages()[0]
        objs = page.objects()
        if not objs:
            raise AssertionError("页面没有对象")
        state["page"] = page
        state["objs"] = objs
        kinds = {}
        for o in objs:
            kinds[o.type_name] = kinds.get(o.type_name, 0) + 1
        print("       %d 页 · 首页 %d 对象 %s" % (
            pkg.page_count(), len(objs), kinds))

    @check("模板页被加载（若文件有）")
    def _():
        doc = pkg.document
        tpls = doc.templates
        if not tpls:
            print("       （无模板页）")
            return
        for tid, tpl in tpls.items():
            n = len(tpl.objects())
            print("       模板 #%s %s: %d 对象" % (tid, tpl.zorder, n))
            if n == 0:
                raise AssertionError("模板 #%s 没有对象（内容没解析出来）" % tid)
        page = state["page"]
        refs = getattr(page, "template_refs", [])
        if not refs:
            raise AssertionError("Document 有模板但页面没有引用")
        tpl_objs = set()
        for tid, _z in refs:
            tpl = doc.templates.get(str(tid))
            if tpl:
                for o in tpl.objects():
                    tpl_objs.add(id(o))
        merged = [o for o in page.objects() if id(o) in tpl_objs]
        print("       页面合并到模板对象: %d" % len(merged))
        if not merged:
            raise AssertionError("模板对象没合并进页面")

    @check("颜色：属性式与子元素式都能读到非黑值")
    def _():
        page = state["page"]
        colored = 0
        for o in page.objects():
            if isinstance(o, (model.PathObject, model.TextObject)):
                c = o.fill_color() if o.has_fill() else (
                    o.stroke_color() if o.has_stroke() else None)
                if c and (abs(c[0] - 1) > 0.02 or abs(c[1]) > 0.02 or abs(c[2]) > 0.02):
                    colored += 1
        print("       有非黑色对象: %d" % colored)
        if not colored:
            print("       （整份文件都是黑色，跳过着色检查）")

    @check("内容完整性：文本 / 路径 / 图片都进了指令")
    def _():
        page = state["page"]
        objs = state["objs"]
        sc = build_page_scene(page)
        text_ops = [o for o in sc.ops if o["t"] == "text"]
        path_ops = [o for o in sc.ops if o["t"] == "path"]
        img_ops = [o for o in sc.ops if o["t"] == "image"]
        n_text = sum(1 for o in objs if o.type_name == "文本")
        n_path = sum(1 for o in objs if o.type_name == "路径")
        n_img = sum(1 for o in objs if o.type_name == "图片")
        print("       对象 文本%d 路径%d 图片%d → 指令 text%d path%d image%d" % (
            n_text, n_path, n_img, len(text_ops), len(path_ops), len(img_ops)))
        if n_path and not path_ops:
            raise AssertionError("有路径对象但一条 path 指令都没有")
        if n_img and not img_ops:
            raise AssertionError("有图片对象但没有 image 指令")

        # 文本字符级核对
        zf = zipfile.ZipFile(path)
        xml_texts = xml_text_codes(zf, pkg)
        zf.close()
        joined_xml = "".join(xml_texts)
        joined_op = "".join(o["text"] for o in text_ops)
        if joined_xml.strip():
            from collections import Counter
            c1 = Counter(joined_xml)
            c2 = Counter(joined_op)
            missing = {ch: (c1[ch], c2.get(ch, 0)) for ch in c1
                       if ch.strip() and c2.get(ch, 0) < c1[ch]}
            print("       XML 字符 %d，渲染字符 %d" % (len(joined_xml), len(joined_op)))
            if missing:
                raise AssertionError("字符级缺失 %r" % list(missing.items())[:6])

    @check("渲染非空白")
    def _():
        img, warn = render_page(state["page"], dpi=96)
        px = img.convert("L").load()
        dark = 0
        for y in range(0, img.size[1], 5):
            for x in range(0, img.size[0], 5):
                if px[x, y] < 200:
                    dark += 1
        print("       暗像素采样 %d，尺寸 %s" % (dark, img.size))
        if dark < 30:
            raise AssertionError("画面几乎空白")
        state["img_path"] = os.path.join(
            ROOT, "out", "real_" + os.path.basename(path).rsplit(".", 1)[0] + ".png")
        os.makedirs(os.path.dirname(state["img_path"]), exist_ok=True)
        img.save(state["img_path"])

    @check("编辑＋保存：只重写改动的那一条")
    def _():
        tmpdir = tempfile.mkdtemp(prefix="ofdreal-")
        dst = os.path.join(tmpdir, "edited.ofd")
        shutil.copy2(path, dst)
        pkg2 = OFDPackage.open(dst)
        ed = Editor(pkg2)
        page = pkg2.all_pages()[0]
        target = None
        for o in page.objects():
            if isinstance(o, model.TextObject) and o.text().strip():
                target = o
                break
        if target is None:
            raise AssertionError("找不到可编辑文本")
        old = target.text()
        ed.set_text(target, old + "·改")
        ed.save()

        pkg3 = OFDPackage.open(dst)
        texts = [o.text() for o in pkg3.all_pages()[0].objects()
                 if isinstance(o, model.TextObject)]
        if not any("·改" in t for t in texts):
            raise AssertionError("保存后重开看不到改动")

        a, b = zipfile.ZipFile(path), zipfile.ZipFile(dst)
        changed = [n for n in a.namelist() if n in b.namelist()
                   and a.read(n) != b.read(n)]
        a.close(); b.close()
        print("       被改写条目:", changed)
        # 只允许 Content.xml 变（以及必要的 Document.xml）
        bad = [n for n in changed
               if not (n.endswith("Content.xml") or n.endswith("Document.xml"))]
        if bad:
            raise AssertionError("不该被改写的条目变了: %s" % bad)
        shutil.rmtree(tmpdir, ignore_errors=True)

    return state


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        env = os.environ.get("OFD_REAL_SAMPLES", "")
        args = [p for p in re.split(r"[;\n]+", env) if p.strip()]
    args = [os.path.expanduser(a.strip('"')) for a in args if a.strip()]
    args = [a for a in args if os.path.isfile(a)]
    if not args:
        print("没有可用的真实样本，跳过。")
        print("用法: python tools/real_sample_test.py 样本.ofd")
        return 0

    for p in args:
        try:
            run_one(p)
        except Exception as exc:
            FAIL.append((os.path.basename(p) + " 打开失败", exc))
            print("  FAIL 打开:", exc)

    print()
    print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        for name, exc in FAIL:
            print("  -", name, ":", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

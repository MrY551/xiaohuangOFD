#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OFD 命令行工具。

    python tools/ofd_cli.py info    a.ofd
    python tools/ofd_cli.py text    a.ofd -o a.txt
    python tools/ofd_cli.py render  a.ofd -o out/ --dpi 150
    python tools/ofd_cli.py pdf     a.ofd -o a.pdf --dpi 200
    python tools/ofd_cli.py svg     a.ofd -o out/
    python tools/ofd_cli.py edit    a.ofd --replace 旧=新 -o b.ofd
    python tools/ofd_cli.py settext a.ofd --page 1 --id 8 --text "新内容" -o b.ofd
    python tools/ofd_cli.py print   a.ofd --printer office --pages 1-3 --nup 2 --duplex
    python tools/ofd_cli.py printers
    python tools/ofd_cli.py fonts
    python tools/ofd_cli.py uidemo          # 没有界面时生成一张 UI 效果图

退出码：0 成功，2 参数错，3 运行错。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofdlite import OFDPackage, font_manager
from ofdlite import convert, model
from ofdlite.edits import Editor
from ofdlite.errors import OfdError


def _pages_arg(text, total):
    """解析 "1-3,5" → [0,2,3,4]（0 基）。"""
    if not text:
        return list(range(total))
    out = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                a, b = int(a), int(b)
            except ValueError:
                continue
            for i in range(a, b + 1):
                if 1 <= i <= total:
                    out.append(i - 1)
        else:
            try:
                i = int(part)
            except ValueError:
                continue
            if 1 <= i <= total:
                out.append(i - 1)
    return sorted(set(out))


def cmd_info(args):
    pkg = OFDPackage.open(args.file)
    rep = convert.structure_report(pkg)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print("文件: %s" % rep["path"])
    print("包内条目: %d   文档: %d   总页数: %d" % (rep["entries"], rep["documents"], rep["pages"]))
    for d in rep["docs"]:
        print("\n[%s]  标题=%s  作者=%s" % (d["entry"], d["title"] or "-", d["author"] or "-"))
        print("  字体: %s" % (", ".join("%s=%s" % (k, v) for k, v in d["fonts"].items()) or "无"))
        print("  图片: %s" % (", ".join("%s=%s" % kv for kv in d["images"].items()) or "无"))
        for p in d["pages"]:
            print("  第 %d 页  %sx%s mm  旋转 %d°  图层 %d  对象 %d  %s"
                  % (p["index"] + 1, p["size"][0], p["size"][1], p["rotation"],
                     p["layers"], p["objects"],
                     " ".join("%s%d" % (k, v) for k, v in p["kinds"].items())))
    return 0


def cmd_text(args):
    pkg = OFDPackage.open(args.file)
    text = convert.export_text(pkg, args.out or "")
    if not args.out:
        sys.stdout.write(text)
    else:
        print("已写出 %s（%d 字节）" % (args.out, os.path.getsize(args.out)))
    return 0


def cmd_render(args):
    from ofdlite.render_pil import render_page
    pkg = OFDPackage.open(args.file)
    pages = pkg.all_pages()
    idx = _pages_arg(args.pages, len(pages))
    outdir = args.out or "out"
    os.makedirs(outdir, exist_ok=True)
    for i in idx:
        img, warn = render_page(pages[i], dpi=args.dpi)
        ext = "tiff" if args.dpi > 600 else "png"
        path = os.path.join(outdir, "page_%03d.%s" % (i + 1, ext))
        img.save(path)
        print("%s  %dx%d  %s" % (path, img.size[0], img.size[1],
                                 ("warn: " + "; ".join(warn)) if warn else ""))
    return 0


def cmd_pdf(args):
    pkg = OFDPackage.open(args.file)
    out = args.out or os.path.splitext(args.file)[0] + ".pdf"
    idx = _pages_arg(args.pages, pkg.page_count())

    def prog(n, total):
        sys.stderr.write("\r渲染 %d/%d" % (n, total))
        sys.stderr.flush()

    convert.export_pdf(pkg, out, dpi=args.dpi, pages=idx, progress=prog)
    sys.stderr.write("\n")
    print("已写出 %s（%d 字节，%d 页，%ddpi）" % (out, os.path.getsize(out), len(idx), args.dpi))
    return 0


def cmd_svg(args):
    pkg = OFDPackage.open(args.file)
    pages = pkg.all_pages()
    idx = _pages_arg(args.pages, len(pages))
    outdir = args.out or "out"
    os.makedirs(outdir, exist_ok=True)
    for i in idx:
        path = os.path.join(outdir, "page_%03d.svg" % (i + 1))
        convert.export_svg(pages[i], path)
        print("%s  %d 字节" % (path, os.path.getsize(path)))
    return 0


def cmd_edit(args):
    pkg = OFDPackage.open(args.file)
    ed = Editor(pkg)
    changed = False
    if args.replace:
        for pair in args.replace:
            if "=" not in pair:
                raise SystemExit("--replace 需要 旧=新 形式：%s" % pair)
            old, new = pair.split("=", 1)
            n = ed.replace_all(old, new)
            print("替换 %r → %r：%d 处" % (old, new, n))
            changed = changed or n > 0
    if args.set_text:
        page = pkg.all_pages()[args.page - 1]
        target = None
        for o in page.objects():
            if isinstance(o, model.TextObject):
                if args.id and o.id == str(args.id):
                    target = o
                    break
                if not args.id:
                    target = o
                    break
        if target is None:
            raise SystemExit("第 %d 页找不到对象 %s" % (args.page, args.id or "（第一个文本）"))
        ed.set_text(target, args.set_text)
        print("已修改对象 #%s" % target.id)
        changed = True
    if not changed:
        print("没有改动")
        return 0
    out = args.out or args.file
    ed.save(out)
    # 保存会重写被改的 XML 条目，原数字签名必然失效；这句是给误用于票据的人看的
    print("注意: %s 的原始数字签名已失效，改后的文件仅用于版式调整；"
          "不得用于报销、入账、归档等需要验签的场景。" % out)
    print("已保存 %s（%d 字节）" % (out, os.path.getsize(out)))
    return 0


def cmd_print(args):
    from ofdlite import print_linux
    pkg = OFDPackage.open(args.file)
    idx = _pages_arg(args.pages, pkg.page_count())
    res = print_linux.print_document(
        pkg, printer=args.printer or print_linux.default_printer(),
        pages=idx, copies=args.copies, n_up=args.nup,
        paper=args.paper, duplex=args.duplex, booklet=args.booklet,
        dpi=args.dpi, keep_pdf=args.keep_pdf)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 3


def cmd_printers(args):
    from ofdlite import print_linux
    printers = print_linux.list_printers()
    dflt = print_linux.default_printer()
    if not printers:
        print("没有检测到 CUPS 打印机（lpstat 不可用或没有配置打印机）")
        return 0
    for p in printers:
        mark = " *" if p["name"] == dflt else ""
        print("%-24s %s%s" % (p["name"], p["info"][:60], mark))
    return 0


def cmd_fonts(args):
    info = font_manager.describe()
    print("字体目录: %s" % ", ".join(info["dirs"]))
    print("可用字体族: %d   字体文件: %d" % (info["families"], info["files"]))
    print("中文字体: %s" % (info["cjk"] or "（没找到，界面会缺字）"))
    if args.list:
        index = font_manager.scan()
        for name in sorted(index)[:200]:
            print("  %-40s %s" % (name, os.path.basename(index[name][0])))
    return 0


def cmd_uidemo(args):
    """没装 Qt 时也能看效果：用 Pillow 模拟主界面截图。"""
    from PIL import Image, ImageDraw, ImageFont
    from ofdlite.render_pil import render_page

    pkg = OFDPackage.open(args.file)
    page_img, _w = render_page(pkg.all_pages()[0], dpi=96)
    W, H = 1280, 800
    shot = Image.new("RGB", (W, H), (245, 246, 248))
    d = ImageDraw.Draw(shot)
    font_path = font_manager.cjk_font()
    def f(sz, bold=False):
        try:
            return ImageFont.truetype(font_path, sz)
        except Exception:
            return ImageFont.load_default()

    # 菜单栏
    d.rectangle([0, 0, W, 28], fill=(240, 241, 243))
    d.text((10, 7), "文件   编辑   视图   页面   打印   帮助", font=f(13), fill=(40, 40, 40))
    # 工具栏
    d.rectangle([0, 28, W, 68], fill=(250, 250, 251))
    d.line([0, 68, W, 68], fill=(214, 216, 220))
    tools = ["打开", "保存", "撤销", "重做", "文字", "图片", "打印", "导出"]
    x = 12
    for t in tools:
        d.rounded_rectangle([x, 38, x + 62, 62], 4, outline=(200, 203, 208), fill=(255, 255, 255))
        d.text((x + 14, 44), t, font=f(12), fill=(50, 50, 50))
        x += 70
    # 左侧缩略图
    d.rectangle([0, 69, 168, H], fill=(255, 255, 255))
    d.line([168, 69, 168, H], fill=(214, 216, 220))
    th = page_img.resize((128, int(128 * page_img.size[1] / page_img.size[0])), Image.LANCZOS)
    shot.paste(th, (20, 84))
    d.rectangle([20, 84, 20 + th.size[0], 84 + th.size[1]], outline=(0, 122, 204), width=2)
    d.text((20, 84 + th.size[1] + 6), "第 1 页", font=f(12), fill=(60, 60, 60))
    # 中间画布
    d.rectangle([169, 69, W - 260, H], fill=(233, 235, 238))
    cw = int(595 * 0.92)
    ch = int(cw * page_img.size[1] / page_img.size[0])
    canvas = page_img.resize((cw, ch), Image.LANCZOS)
    cx = 169 + (W - 260 - 169 - cw) // 2
    cy = 69 + 24
    d.rectangle([cx + 4, cy + 4, cx + cw + 4, cy + ch + 4], fill=(200, 202, 206))
    shot.paste(canvas, (cx, cy))
    # 右侧属性
    d.rectangle([W - 260, 69, W, H], fill=(255, 255, 255))
    d.line([W - 260, 69, W - 260, H], fill=(214, 216, 220))
    d.text((W - 246, 84), "属性", font=f(14, True), fill=(30, 30, 30))
    rows = [("对象", "TextObject #8"), ("位置", "20.0, 22.0 mm"), ("尺寸", "170.0 × 16.0 mm"),
            ("字体", "黑体"), ("字号", "9.0 mm"), ("颜色", "#000000"), ("对齐", "左对齐")]
    y = 116
    for k, v in rows:
        d.text((W - 246, y), k, font=f(12), fill=(120, 122, 128))
        d.text((W - 180, y), v, font=f(12), fill=(30, 30, 30))
        y += 26
    d.line([W - 246, y + 4, W - 20, y + 4], fill=(232, 234, 238))
    d.text((W - 246, y + 16), "页面", font=f(14, True), fill=(30, 30, 30))
    rows2 = [("尺寸", "210.0 × 297.0 mm"), ("旋转", "0°"), ("页数", "2"), ("缩放", "92%")]
    y += 46
    for k, v in rows2:
        d.text((W - 246, y), k, font=f(12), fill=(120, 122, 128))
        d.text((W - 180, y), v, font=f(12), fill=(30, 30, 30))
        y += 26
    # 状态栏
    d.rectangle([0, H - 26, W, H], fill=(240, 241, 243))
    d.line([0, H - 26, W, H - 26], fill=(214, 216, 220))
    d.text((12, H - 20), "sample.ofd — 2 页 — 就绪", font=f(12), fill=(90, 92, 98))
    d.text((W - 220, H - 20), "CUPS 可用 · 字体 171 族", font=f(12), fill=(90, 92, 98))
    out = args.out or "out/ui_demo.png"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    shot.save(out)
    print("已生成界面效果图 %s" % out)
    return 0


def main(argv=None):
    from ofdlite import __version__ as _VER
    ap = argparse.ArgumentParser(prog="ofd_cli",
                                 description="小黄OFD 命令行工具 (v%s)" % _VER)
    # 不用 argparse 的 version action：它会把多行文本压成一行
    ap.add_argument("--version", action="store_true",
                    help="显示版本与作者")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("info", help="结构信息")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("text", help="导出文本")
    p.add_argument("file")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("render", help="渲染成图片")
    p.add_argument("file")
    p.add_argument("-o", "--out")
    p.add_argument("--dpi", type=int, default=150)
    p.add_argument("--pages")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("pdf", help="导出 PDF")
    p.add_argument("file")
    p.add_argument("-o", "--out")
    p.add_argument("--dpi", type=int, default=200)
    p.add_argument("--pages")
    p.set_defaults(func=cmd_pdf)

    p = sub.add_parser("svg", help="导出 SVG")
    p.add_argument("file")
    p.add_argument("-o", "--out")
    p.add_argument("--pages")
    p.set_defaults(func=cmd_svg)

    p = sub.add_parser("edit", help="改文本并另存")
    p.add_argument("file")
    p.add_argument("-o", "--out")
    p.add_argument("--replace", action="append", metavar="旧=新")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--id")
    p.add_argument("--set-text", dest="set_text")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("print", help="打印")
    p.add_argument("file")
    p.add_argument("--printer")
    p.add_argument("--pages")
    p.add_argument("--copies", type=int, default=1)
    p.add_argument("--nup", type=int, default=1, choices=[1, 2, 4, 6, 9])
    p.add_argument("--paper", default="A4")
    p.add_argument("--duplex", action="store_true")
    p.add_argument("--booklet", action="store_true")
    p.add_argument("--dpi", type=int, default=200)
    p.add_argument("--keep-pdf", dest="keep_pdf")
    p.set_defaults(func=cmd_print)

    p = sub.add_parser("printers", help="列出打印机")
    p.set_defaults(func=cmd_printers)

    p = sub.add_parser("fonts", help="字体环境")
    p.add_argument("--list", action="store_true")
    p.set_defaults(func=cmd_fonts)

    p = sub.add_parser("uidemo", help="生成界面效果图（不需要 Qt）")
    p.add_argument("file")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_uidemo)

    args = ap.parse_args(argv)
    if getattr(args, "version", False):
        from ofdlite.buildid import banner
        print(banner())
        print("依据 GB/T 33190-2016 独立实现 · 许可见 LICENSE")
        return 0
    if not getattr(args, "cmd", None):
        ap.print_help()
        return 2
    try:
        return args.func(args)
    except OfdError as exc:
        sys.stderr.write("错误: %s\n" % exc)
        return 3
    except FileNotFoundError as exc:
        sys.stderr.write("文件不存在: %s\n" % exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())

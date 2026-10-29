# -*- coding: utf-8 -*-
"""Tkinter 桌面端可行性验证（不依赖 Qt）。

思路：GUI 外壳用标准库 tkinter（PSF 许可），页面渲染仍走 ofdlite.render_pil
（Pillow，MIT-CMU，3× 超采样），所以视觉质量和 Qt 版一致——Qt 只是画到
QPainter，这里是把同一张位图贴到 Canvas 上。

验证四件事：
  1. 大图（A4@150dpi ≈ 1240×1754）能正常显示、能滚动
  2. 点击坐标 → 页面毫米坐标 → 命中对象
  3. 缩放（适应窗口 / 100%）
  4. 截图，和 Qt 版对比

    python tools/tk_probe.py 文件.ofd [--shot out/tk_probe.png] [--hold 秒]
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--shot", default="")
    ap.add_argument("--hold", type=float, default=2.5, help="自动关闭前停留秒数")
    args = ap.parse_args(argv)

    import tkinter as tk
    from tkinter import ttk
    from PIL import Image, ImageTk

    from ofdlite import OFDPackage
    from ofdlite.render_pil import render_page

    pkg = OFDPackage.open(args.file)
    page = pkg.all_pages()[0]
    img, warns = render_page(page, dpi=args.dpi)
    if warns:
        print("渲染告警:", warns[:3])
    px_per_mm = args.dpi / 25.4
    size = page.size()
    print("页面: %.1f × %.1f mm" % (size.w, size.h))
    print("位图: %d × %d px @ %ddpi" % (img.width, img.height, args.dpi))

    root = tk.Tk()
    root.title("小黄OFD — Tk 原型")
    root.geometry("980x760+80+60")
    # 截图要抓窗口区域，先确保它在前台
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()

    # 顶部工具条
    bar = ttk.Frame(root, padding=(8, 6))
    bar.pack(side="top", fill="x")
    ttk.Label(bar, text=os.path.basename(args.file)).pack(side="left")
    info = tk.StringVar(value="点页面上的对象试试")
    ttk.Label(bar, textvariable=info, foreground="#555").pack(side="right")

    # Canvas + 双滚动条
    body = ttk.Frame(root)
    body.pack(side="top", fill="both", expand=True)
    canvas = tk.Canvas(body, background="#3c3c3c", highlightthickness=0)
    vsb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
    hsb = ttk.Scrollbar(body, orient="horizontal", command=canvas.xview)
    canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    body.rowconfigure(0, weight=1)
    body.columnconfigure(0, weight=1)

    photo = ImageTk.PhotoImage(img)
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas.configure(scrollregion=(0, 0, img.width, img.height))

    hits = {"n": 0}

    def on_click(ev):
        # Canvas 坐标（考虑滚动）→ 像素 → 毫米 → 页面命中
        cx = canvas.canvasx(ev.x)
        cy = canvas.canvasy(ev.y)
        mx, my = cx / px_per_mm, cy / px_per_mm
        obj = page.hit_test(mx, my)
        hits["n"] += 1
        if obj is None:
            info.set("(%.1f, %.1f) mm → 空白" % (mx, my))
        else:
            text = ""
            try:
                if getattr(obj, "text", None):
                    t = obj.text()
                    text = "「%s」" % (t[:18] if t else "")
            except Exception:
                pass
            info.set("(%.1f, %.1f) mm → %s #%s %s" % (
                mx, my, obj.TYPE, obj.id, text))

    canvas.bind("<Button-1>", on_click)

    def fit(*_):
        """适应窗口宽度。"""
        w = canvas.winfo_width()
        if w > 10:
            z = max(0.05, min(3.0, w / float(img.width)))
            canvas.itemconfigure(1, image=photo)
            canvas.scale("all", 0, 0, z / getattr(fit, "last", 1.0), z / getattr(fit, "last", 1.0))
            fit.last = z
            canvas.configure(scrollregion=canvas.bbox("all"))

    def save_shot():
        try:
            from PIL import ImageGrab
            root.update_idletasks()
            root.update()
            x, y = root.winfo_rootx(), root.winfo_rooty()
            w, h = root.winfo_width(), root.winfo_height()
            im = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            im.save(args.shot)
            print("截图:", args.shot, im.size)
        except Exception as exc:
            print("截图失败:", exc)

    # 演示用：先模拟一次点击（命中页面中部的一个对象），再截图
    def demo_click():
        """用对象自己的 boundary 中心反推画布坐标，验证坐标链路。"""
        from ofdlite import model
        objs = [o for o in page.objects() if isinstance(o, model.TextObject)]
        if not objs:
            return
        o = objs[min(6, len(objs) - 1)]
        b = o.boundary()
        mx, my = b.x + b.w / 2.0, b.y + b.h / 2.0
        px, py = int(mx * px_per_mm), int(my * px_per_mm)
        got = page.hit_test(mx, my)
        msg = "(%.1f, %.1f) mm → %s #%s「%s」" % (
            mx, my, got.TYPE if got else "空白",
            got.id if got else "-", (o.text() or "")[:16])
        info.set(msg)
        print("坐标链路:", msg)
        print("反向映射回画布:", (px, py), "画布反算 mm:", (px / px_per_mm, py / px_per_mm))

    root.after(300, demo_click)
    if args.shot:
        root.after(900, save_shot)
    root.after(int(args.hold * 1000), root.destroy)
    root.mainloop()
    print("命中次数:", hits["n"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

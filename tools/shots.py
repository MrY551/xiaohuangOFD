# -*- coding: utf-8 -*-
"""生成界面截图（桌面端 + 打印拼版预览）。

    QT_QPA_PLATFORM=offscreen python3 tools/shots.py [--out out]

产出：
    shot_desktop_read.png   桌面端：第 1 页，选中标题，属性面板填满
    shot_desktop_edit.png   桌面端：改完文字 + 插入方框后的第 2 页
    shot_print_nup2.png     打印拼版预览：2 合 1 双面排到 A4 横向
"""
from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QFontMetricsF
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from ofdlite import OFDPackage
from ofdlite import geometry as geo


def click_mm(canvas, x_mm, y_mm):
    ox, oy = canvas._origin()
    px = int(round(ox + x_mm * canvas.zoom))
    py = int(round(oy + y_mm * canvas.zoom))
    QTest.mouseClick(canvas, Qt.LeftButton, Qt.NoModifier, QPoint(px, py))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "out"))
    ap.add_argument("--sample", default=os.path.join(ROOT, "out", "sample.ofd"))
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    if not os.path.exists(args.sample):
        import make_sample_ofd
        make_sample_ofd.build(args.sample)

    from app.main import build_stylesheet
    from app.mainwindow import MainWindow

    app = QApplication([])
    app.setStyleSheet(build_stylesheet())
    win = MainWindow()
    win.resize(1360, 880)
    win.show()
    win.load_document(os.path.abspath(args.sample))
    win._build_thumbs()
    app.processEvents()

    # ---------------------------------------------------------- 阅读态
    win.goto_page(0)
    app.processEvents()
    win.fit_zoom()
    app.processEvents()
    # 点标题文字：让属性面板显示出文字/字号/颜色/字体
    click_mm(win.canvas, 60, 30)
    app.processEvents()
    if not win.canvas.selection:
        # 兜底：直接选中第一个文本对象
        page = win.pkg.all_pages()[0]
        from ofdlite.scene import selection_boxes
        objs = [o for o in page.objects() if o.type_name == "文本"]
        if objs:
            win.canvas.selection = selection_boxes(page, objs[:1])
            win.on_selection_changed(win.canvas.selection)
    app.processEvents()
    path = os.path.join(args.out, "shot_desktop_read.png")
    win.grab().save(path)
    print("写出", path, os.path.getsize(path), "字节")

    # ---------------------------------------------------------- 编辑态
    win.goto_page(1)
    app.processEvents()
    win.fit_zoom()
    app.processEvents()
    # 改一个字、插一个方框，展示"改过"的状态
    try:
        page = win.pkg.all_pages()[1]
        from ofdlite import model as M
        texts = [o for o in page.objects() if isinstance(o, M.TextObject) and "表格" in o.text()]
        if texts:
            win.apply_edit("set_text", id=texts[0].id,
                           text="第二页：编辑后的表格与字距")
            app.processEvents()
        win.add_rect_at(126, 132, 188, 150)
        app.processEvents()
    except Exception as exc:
        print("编辑态构造失败（不影响其它截图）:", exc)
    win.goto_page(1)
    app.processEvents()
    win.fit_zoom()
    app.processEvents()
    click_mm(win.canvas, 40, 48)
    app.processEvents()
    path = os.path.join(args.out, "shot_desktop_edit.png")
    win.grab().save(path)
    print("写出", path, os.path.getsize(path), "字节")

    # ---------------------------------------------------------- 拼版预览
    build_nup_preview(win.pkg, [0, 1], os.path.join(args.out, "shot_print_nup2.png"))
    return 0


def build_nup_preview(pkg, pages, out_path, n_up=2, paper="A4", dpi=110,
                      margin_mm=10.0, gap_mm=6.0):
    """按打印拼版的实际网格参数，把页面画到一张横向 A4 上，出预览图。"""
    from ofdlite.print_linux import paper_size
    from ofdlite.render_pil import PilRenderer
    from ofdlite.scene import build_page_scene

    pw, ph = paper_size(paper)
    pw, ph = ph, pw                     # 横向
    cols, rows = (2, 1) if n_up == 2 else (1, 1)
    W = int(round(pw * dpi / 25.4))
    H = int(round(ph * dpi / 25.4))
    canvas = QImage(W, H, QImage.Format_RGB32)
    canvas.fill(QColor(255, 255, 255))

    from app.qt_renderer import QtSceneRenderer
    renderer = QtSceneRenderer()
    p = QPainter(canvas)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    zoom = dpi / 25.4
    cell_w = (pw - 2 * margin_mm - gap_mm * (cols - 1)) / cols
    cell_h = (ph - 2 * margin_mm - gap_mm * (rows - 1)) / rows

    all_pages = pkg.all_pages()
    for k, pidx in enumerate(pages[:n_up]):
        if pidx is None or pidx >= len(all_pages):
            continue
        sc = build_page_scene(all_pages[pidx]).to_dict()
        size = sc["size"]
        r, c = divmod(k, cols)
        cx = margin_mm + c * (cell_w + gap_mm)
        cy = margin_mm + r * (cell_h + gap_mm)
        k_scale = min(cell_w / size[0], cell_h / size[1])
        draw_w = size[0] * k_scale
        draw_h = size[1] * k_scale
        ox = (cx + (cell_w - draw_w) / 2) * zoom
        oy = (cy + (cell_h - draw_h) / 2) * zoom
        p.save()
        p.translate(ox, oy)
        p.scale(1, 1)
        renderer.render(p, sc, zoom * k_scale, clip_outline=False)
        p.restore()
        # 版面参考线：虚线框标出每个页位
        pen = p.pen()
        pen.setColor(QColor(0, 114, 198, 90))
        pen.setStyle(Qt.DashLine)
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(int(cx * zoom), int(cy * zoom),
                   int(cell_w * zoom), int(cell_h * zoom))

    # 页脚标注
    f = QFont("Noto Sans CJK SC")
    f.setPixelSize(20)
    p.setFont(f)
    p.setPen(QColor(120, 124, 130))
    p.drawText(24, H - 18, "A4 横向 · 2 合 1 · 双面长边翻页 · 页位虚线框仅预览用")
    p.end()

    canvas.save(out_path)
    print("写出", out_path, os.path.getsize(out_path), "字节")


if __name__ == "__main__":
    sys.exit(main())

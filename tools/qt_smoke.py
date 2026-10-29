# -*- coding: utf-8 -*-
"""Qt 桌面端冒烟测试（不需要真实显示器）。

    QT_QPA_PLATFORM=offscreen python tools/qt_smoke.py --shot out/qt_shot.png

做的事：
  1. 建窗口、打开样本
  2. 抓窗口截图（offscreen 下 grab 可用）
  3. 走一遍编辑：改文字 / 移动 / 插入 / 撤销 / 插页
  4. 用 QPrinter 打到 PDF，验证打印链路
  5. 检查撤销栈与页面模型一致性
退出码 0 = 全过。
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
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
            traceback.print_exc()
        return fn
    return deco


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", default=os.path.join(ROOT, "out", "qt_shot.png"))
    ap.add_argument("--sample", default=os.path.join(ROOT, "out", "sample.ofd"))
    args = ap.parse_args(argv)

    if not os.path.exists(args.sample):
        import make_sample_ofd
        os.makedirs(os.path.dirname(args.sample), exist_ok=True)
        make_sample_ofd.build(args.sample)

    from PySide6.QtCore import QSize, Qt, QTimer
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from app.main import build_stylesheet
    from app.mainwindow import MainWindow
    from app.qt_renderer import QT_API, QtSceneRenderer, render_page_to_image
    from ofdlite import OFDPackage, build_page_scene, model
    from ofdlite.edits import Editor

    app = QApplication([])
    app.setStyleSheet(build_stylesheet())
    print("Qt 绑定:", QT_API)

    win = MainWindow()
    win.resize(1280, 840)
    win.show()

    state = {}

    @check("窗口创建并加载文档")
    def _():
        win.load_document(os.path.abspath(args.sample))
        if win.pkg is None or win.pkg.page_count() != 2:
            raise AssertionError("页数 %r" % (win.pkg.page_count() if win.pkg else None))
        state["pkg"] = win.pkg

    @check("大纲树填充与跳页索引")
    def _():
        # 7.8：换一份带大纲的文档，验证树里有项且首项带跳页索引
        ol_path = os.path.join(ROOT, "out", "ol_test.ofd")
        if not os.path.exists(ol_path):
            print("       （跳过：out/ol_test.ofd 不存在）")
            return
        win.load_document(ol_path)
        app.processEvents()
        n = win.outline.topLevelItemCount()
        if n < 1:
            raise AssertionError("大纲树为空")
        idx = win.outline.topLevelItem(0).data(0, Qt.UserRole)
        if idx != 0:
            raise AssertionError("首项跳页索引 %r，期望 0" % idx)
        if win.outline.topLevelItem(0).childCount() < 1:
            raise AssertionError("子项没填进去")
        # 换回原样本，后面的用例还要用
        win.load_document(os.path.abspath(args.sample))
        app.processEvents()

    @check("画布拿到场景指令")
    def _():
        sc = win.canvas.scene
        if not sc or not sc.get("ops"):
            raise AssertionError("场景为空")
        kinds = set(op["t"] for op in sc["ops"])
        for need in ("text", "path", "image"):
            if need not in kinds:
                raise AssertionError(kinds)
        if not sc.get("objects"):
            raise AssertionError("objects 为空")

    @check("离屏渲染出非空白图像")
    def _():
        sc = build_page_scene(win.pkg.all_pages()[0]).to_dict()
        img = render_page_to_image(sc, zoom=96 / 25.4)
        if img.width() < 700 or img.height() < 1000:
            raise AssertionError(img.size())
        # 统计非白像素
        nonwhite = 0
        for y in range(0, img.height(), 9):
            for x in range(0, img.width(), 9):
                c = img.pixelColor(x, y)
                if c.red() < 240 or c.green() < 240 or c.blue() < 240:
                    nonwhite += 1
        if nonwhite < 40:
            raise AssertionError("画面几乎空白 nonwhite=%d" % nonwhite)
        state["img"] = img

    @check("改文字并刷新")
    def _():
        page = win.pkg.all_pages()[0]
        obj = [o for o in page.objects() if isinstance(o, model.TextObject)][0]
        state["obj_id"] = obj.id
        win.apply_edit("set_text", id=obj.id, text="桌面端改的标题")
        objs = [o for o in win.pkg.all_pages()[0].objects()
                if isinstance(o, model.TextObject)]
        hit = [o for o in objs if o.id == obj.id]
        if not hit or hit[0].text() != "桌面端改的标题":
            raise AssertionError(hit and hit[0].text())

    @check("移动对象")
    def _():
        page = win.pkg.all_pages()[0]
        obj = [o for o in page.objects() if isinstance(o, model.PathObject)][0]
        before = obj.world_bounds()
        win.apply_edit("move", ids=[obj.id], dx=6, dy=4)
        after = [o for o in win.pkg.all_pages()[0].objects() if o.id == obj.id][0].world_bounds()
        if abs((after.x - before.x) - 6) > 0.2 or abs((after.y - before.y) - 4) > 0.2:
            raise AssertionError((before, after))

    @check("插入文字 / 方框")
    def _():
        n0 = len(win.pkg.all_pages()[0].objects())
        win.add_text_at(20, 270)
        win.add_rect_at(30, 150, 90, 180)
        n1 = len(win.pkg.all_pages()[0].objects())
        if n1 != n0 + 2:
            raise AssertionError((n0, n1))

    @check("撤销 / 重做")
    def _():
        n_before = len(win.pkg.all_pages()[0].objects())
        win.undo()
        n_after = len(win.pkg.all_pages()[0].objects())
        if n_after != n_before - 1:
            raise AssertionError((n_before, n_after))
        win.redo()
        if len(win.pkg.all_pages()[0].objects()) != n_before:
            raise AssertionError("重做没恢复")

    @check("插页 / 删页")
    def _():
        n0 = win.pkg.page_count()
        win.insert_page()
        if win.pkg.page_count() != n0 + 1:
            raise AssertionError((n0, win.pkg.page_count()))
        win.delete_page()
        if win.pkg.page_count() != n0:
            raise AssertionError("删页失败")

    @check("缩略图生成")
    def _():
        win._build_thumbs()
        item = win.thumbs.list.item(0)
        if item is None or item.icon().isNull():
            raise AssertionError("缩略图为空")

    @check("窗口截图")
    def _():
        if win.pkg.page_count() > 1:
            win.goto_page(1)          # 截第 2 页：表格/字距/旋转都在这一页
        win.grab().save(args.shot)
        if os.path.getsize(args.shot) < 5000:
            raise AssertionError("截图太小")
        print("     截图:", args.shot)

    @check("QPdfWriter 矢量打印链路")
    def _():
        from PySide6.QtGui import QPdfWriter
        from PySide6.QtCore import QMarginsF
        from PySide6.QtGui import QPageLayout, QPageSize
        from app.qt_renderer import QtSceneRenderer
        from ofdlite.scene import build_page_scene

        out = os.path.join(ROOT, "out", "qt_print.pdf")
        writer = QPdfWriter(out)
        writer.setPageSize(QPageSize(QPageSize.A4))
        writer.setResolution(300)
        writer.setPageMargins(QMarginsF(0, 0, 0, 0))
        r = QtSceneRenderer()
        for i, page in enumerate(win.pkg.all_pages()):
            if i:
                writer.newPage()
            p = QPainter(writer)
            sc = build_page_scene(page).to_dict()
            size = sc["size"]
            dpi = 300.0
            zoom = dpi / 25.4
            p.scale(1.0, 1.0)
            r.render(p, sc, zoom, clip_outline=False)
            p.end()
        if not os.path.exists(out) or os.path.getsize(out) < 3000:
            raise AssertionError("PDF 太小")

    app.quit()

    print()
    print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        for name, exc in FAIL:
            print("  -", name, ":", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

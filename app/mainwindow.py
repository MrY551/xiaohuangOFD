# -*- coding: utf-8 -*-
"""主窗口。

布局（左右中三栏，与网页端一致）：
    左：缩略图        中：页面画布       右：属性
顶部菜单 + 工具栏，底部状态栏。
所有编辑都走 ofdlite.edits.Editor，撤销栈是自建的（不用 QUndoStack，
因为 Editor 的快照机制已经保证了正确性，直接接按钮与快捷键）。
"""
from __future__ import annotations

import os
from typing import List, Optional

from .qt_renderer import (QT_API, QtSceneRenderer, qcolor, render_page_to_image)

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QFont, QImage, QKeySequence, QPainter,
                           QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox,
    QDialog, QDialogButtonBox, QDockWidget, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter,
    QStatusBar, QToolBar, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ofdlite import OFDPackage, build_page_scene, model
from ofdlite import geometry as geo
from ofdlite.edits import Editor, EditError
from ofdlite.errors import OfdError

APP_TITLE = "小黄OFD"


# ==================================================================== 画布

class PageCanvas(QWidget):
    """页面绘制 + 交互。内部自己维护 zoom（像素/毫米）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene: dict = {}
        self.renderer = QtSceneRenderer()
        self.zoom = 3.78
        self.selection: List[dict] = []
        self.preview = None           # {"ids":set, "dx":,"dy":}
        self.dragging = None
        self.placing = None
        self.mode = "select"
        self.window_ref = None        # MainWindow，用来回调编辑
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(200, 200)

    # ---------------------------------------------------------- 场景
    def set_scene(self, scene: dict) -> None:
        self.scene = scene or {}
        self.renderer.load_resources(self.scene.get("resources") or {})
        self._refresh_size()

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(0.5, min(40.0, zoom))
        self._refresh_size()

    def _refresh_size(self) -> None:
        s = self.scene.get("size") or [210, 297]
        w = int(s[0] * self.zoom) + 48
        h = int(s[1] * self.zoom) + 48
        self.setFixedSize(max(120, w), max(120, h))
        self.update()

    def page_size(self):
        s = self.scene.get("size") or [210, 297]
        return s[0], s[1]

    # ---------------------------------------------------------- 坐标
    def _origin(self):
        s = self.scene.get("size") or [210, 297]
        w = s[0] * self.zoom
        h = s[1] * self.zoom
        return (self.width() - w) / 2.0, 24.0

    def to_mm(self, pos) -> tuple:
        ox, oy = self._origin()
        return ((pos.x() - ox) / self.zoom, (pos.y() - oy) / self.zoom)

    # ---------------------------------------------------------- 绘制
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(233, 235, 238))
        if not self.scene:
            p.setPen(QColor(140, 145, 152))
            p.drawText(self.rect(), Qt.AlignCenter, "没有打开文档")
            return
        ox, oy = self._origin()
        s = self.scene.get("size") or [210, 297]
        # 阴影
        p.fillRect(QRectF(ox + 4, oy + 4, s[0] * self.zoom, s[1] * self.zoom),
                   QColor(0, 0, 0, 26))
        p.save()
        p.translate(ox, oy)
        self.renderer.render(p, self.scene, self.zoom, preview=self.preview)
        p.restore()
        self._paint_selection(p, ox, oy)
        self._paint_placing(p, ox, oy)

    def _paint_selection(self, p: QPainter, ox, oy):
        if not self.selection:
            return
        pen = QPen(QColor(0, 114, 198))
        pen.setWidthF(1.4)
        pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        dx = self.preview["dx"] if self.preview else 0
        dy = self.preview["dy"] if self.preview else 0
        for o in self.selection:
            pts = o.get("corners") or []
            if len(pts) != 4:
                continue
            poly = QPolygonF([QPointF((x + dx) * self.zoom + ox,
                                      (y + dy) * self.zoom + oy) for x, y in pts])
            p.drawPolygon(poly)
            # 四角小方块
            p.setBrush(QColor(255, 255, 255))
            for x, y in pts:
                cx = (x + dx) * self.zoom + ox
                cy = (y + dy) * self.zoom + oy
                p.drawRect(QRectF(cx - 3, cy - 3, 6, 6))
            p.setBrush(Qt.NoBrush)

    def _paint_placing(self, p: QPainter, ox, oy):
        if not self.placing:
            return
        x0, y0, x1, y1 = self.placing
        pen = QPen(QColor(0, 114, 198))
        pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(min(x0, x1) * self.zoom + ox, min(y0, y1) * self.zoom + oy,
                          abs(x1 - x0) * self.zoom, abs(y1 - y0) * self.zoom))

    # ---------------------------------------------------------- 交互
    def hit(self, x: float, y: float):
        objs = self.scene.get("objects") or []
        for o in reversed(objs):
            pts = o.get("corners") or []
            if len(pts) == 4 and _point_in_quad(x, y, pts):
                return o
        return None

    def mousePressEvent(self, ev):
        if not self.scene or self.window_ref is None:
            return
        x, y = self.to_mm(ev.pos())
        if ev.button() != Qt.LeftButton:
            return
        if self.mode == "place-text":
            self.window_ref.add_text_at(x, y)
            return
        if self.mode == "place-rect":
            self.placing = (x, y, x, y)
            self.update()
            return
        if self.mode == "place-image" and getattr(self.window_ref, "pending_image", None):
            self.window_ref.add_image_at(x, y)
            return
        hit = self.hit(x, y)
        ids = [o["id"] for o in self.selection]
        if hit is None:
            self.selection = []
            self.update()
            self.window_ref.on_selection_changed(self.selection)
            return
        if hit["id"] not in ids:
            if ev.modifiers() & Qt.ShiftModifier:
                self.selection.append(hit)
            else:
                self.selection = [hit]
        self.dragging = {"x0": x, "y0": y, "dx": 0.0, "dy": 0.0, "moved": False}
        self.update()
        self.window_ref.on_selection_changed(self.selection)

    def mouseMoveEvent(self, ev):
        if not self.scene:
            return
        x, y = self.to_mm(ev.pos())
        if self.placing:
            self.placing = (self.placing[0], self.placing[1], x, y)
            self.update()
            return
        if not self.dragging:
            return
        dx = x - self.dragging["x0"]
        dy = y - self.dragging["y0"]
        if abs(dx) > 0.1 or abs(dy) > 0.1:
            self.dragging["moved"] = True
        self.dragging["dx"] = dx
        self.dragging["dy"] = dy
        self.preview = {"ids": set(o["id"] for o in self.selection),
                        "dx": dx, "dy": dy}
        self.update()

    def mouseReleaseEvent(self, ev):
        if self.placing:
            x0, y0, x1, y1 = self.placing
            self.placing = None
            if abs(x1 - x0) > 1 and abs(y1 - y0) > 1:
                self.window_ref.add_rect_at(x0, y0, x1, y1)
            self.update()
            return
        if not self.dragging:
            return
        d = self.dragging
        self.dragging = None
        self.preview = None
        if d["moved"] and self.selection and self.window_ref:
            self.window_ref.move_selection(d["dx"], d["dy"])
        else:
            self.update()

    def mouseDoubleClickEvent(self, ev):
        if not self.scene or self.window_ref is None:
            return
        x, y = self.to_mm(ev.pos())
        hit = self.hit(x, y)
        if hit and hit.get("type") == "文本":
            self.selection = [hit]
            self.update()
            self.window_ref.on_selection_changed(self.selection)
            self.window_ref.edit_text_dialog(hit)

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            factor = 1.1 if ev.angleDelta().y() > 0 else 1 / 1.1
            self.window_ref.set_zoom(self.zoom * factor)
            ev.accept()
            return
        super().wheelEvent(ev)


def _point_in_quad(x, y, quad) -> bool:
    inside = False
    j = 3
    for i in range(4):
        xi, yi = quad[i]
        xj, yj = quad[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


# ==================================================================== 属性

class PropsPanel(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        self.title = QLabel("属性")
        f = QFont()
        f.setBold(True)
        self.title.setFont(f)
        lay.addWidget(self.title)

        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignLeft)
        self.form.setSpacing(6)
        lay.addLayout(self.form)

        self.text_edit = QLineEdit()
        self.text_edit.editingFinished.connect(self._apply_text)
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(0.5, 300)
        self.size_spin.setSingleStep(0.5)
        self.size_spin.setSuffix(" mm")
        self.size_spin.editingFinished.connect(self._apply_style)
        self.color_btn = QPushButton("颜色…")
        self.color_btn.clicked.connect(self._pick_color)
        self.bold_box = QCheckBox("加粗")
        self.bold_box.clicked.connect(self._apply_style)
        self.italic_box = QCheckBox("斜体")
        self.italic_box.clicked.connect(self._apply_style)
        self.font_combo = QComboBox()
        self.font_combo.currentIndexChanged.connect(self._apply_style)

        self.row_text = self._add_row("文字", self.text_edit)
        self.row_size = self._add_row("字号", self.size_spin)
        self.row_color = self._add_row("颜色", self.color_btn)
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.bold_box)
        hl.addWidget(self.italic_box)
        self.row_style = self._add_row("字形", row)
        self.row_font = self._add_row("字体", self.font_combo)

        lay.addWidget(_hline())
        self.pos_label = QLabel("—")
        self.size_label = QLabel("—")
        self.id_label = QLabel("—")
        self.type_label = QLabel("—")
        self._info_widgets = []
        for cap, w in (("位置", self.pos_label), ("尺寸", self.size_label),
                       ("编号", self.id_label), ("类型", self.type_label)):
            row = _label_row(cap, w)
            lay.addWidget(row)
            self._info_widgets.append(row)

        lay.addStretch(1)
        self._current = None
        self.set_selection([])

    def _add_row(self, label, widget):
        self.form.addRow(QLabel(label), widget)
        return label

    def _set_row_visible(self, label, visible):
        lbl = self.form.labelForField(self.form.findChild(type(QWidget()), label)) \
            if False else None
        # 直接控制控件可见性（QFormLayout 会跟着隐藏标签）
        for i in range(self.form.rowCount()):
            if self.form.itemAt(i, QFormLayout.LabelRole) and \
                    self.form.itemAt(i, QFormLayout.LabelRole).widget() and \
                    self.form.itemAt(i, QFormLayout.LabelRole).widget().text() == label:
                for role in (QFormLayout.LabelRole, QFormLayout.FieldRole):
                    item = self.form.itemAt(i, role)
                    if item and item.widget():
                        item.widget().setVisible(visible)
                break
        del lbl

    # ---------------------------------------------------------- 选择
    def set_selection(self, sel: List[dict]) -> None:
        self._current = sel[0] if sel else None
        is_text = bool(self._current and self._current.get("type") == "文本")
        for label in ("文字", "字号", "颜色", "字形", "字体"):
            self._set_row_visible(label, is_text)
        for w in self._info_widgets:
            w.setVisible(bool(sel))
        if not sel:
            self.type_label.setText("—")
            self.id_label.setText("—")
            self.pos_label.setText("—")
            self.size_label.setText("—")
            self.title.setText("属性 · 没有选中对象")
            return
        o = sel[0]
        self.title.setText("属性（选中 %d 个）" % len(sel) if len(sel) > 1 else "属性")
        self.type_label.setText(o.get("type") or "")
        self.id_label.setText("#" + str(o.get("id")))
        pts = o.get("corners") or []
        if len(pts) == 4:
            x0 = min(p[0] for p in pts)
            y0 = min(p[1] for p in pts)
            x1 = max(p[0] for p in pts)
            y1 = max(p[1] for p in pts)
            self.pos_label.setText("%.1f, %.1f mm" % (x0, y0))
            self.size_label.setText("%.1f × %.1f mm" % (x1 - x0, y1 - y0))
        if is_text:
            self._block(True)
            self.text_edit.setText(o.get("text") or "")
            self.size_spin.setValue(float(o.get("size") or 5))
            self.bold_box.setChecked(bool(o.get("bold")))
            self.italic_box.setChecked(bool(o.get("italic")))
            self.font_combo.clear()
            self.font_combo.addItem("（保持）", "")
            for fid, name in (self.win.fonts or {}).items():
                self.font_combo.addItem("%s (#%s)" % (name, fid), str(fid))
            idx = self.font_combo.findData(str(o.get("font_id") or ""))
            self.font_combo.setCurrentIndex(idx if idx >= 0 else 0)
            c = o.get("color") or [0, 0, 0, 1]
            self.color_btn.setStyleSheet(
                "background:#%02x%02x%02x;color:%s" % (
                    int(c[0] * 255), int(c[1] * 255), int(c[2] * 255),
                    "#fff" if (c[0] + c[1] + c[2]) < 1.5 else "#000"))
            self._block(False)

    def _block(self, on):
        for w in (self.text_edit, self.size_spin, self.bold_box, self.italic_box,
                  self.font_combo):
            w.blockSignals(on)

    # ---------------------------------------------------------- 应用
    def _apply_text(self):
        if not self._current or self.win.editor is None:
            return
        text = self.text_edit.text()
        if text == (self._current.get("text") or ""):
            return
        self.win.apply_edit("set_text", id=self._current["id"], text=text)

    def _apply_style(self):
        if not self._current or self.win.editor is None:
            return
        kwargs = {
            "size": self.size_spin.value(),
            "bold": self.bold_box.isChecked(),
            "italic": self.italic_box.isChecked(),
        }
        fid = self.font_combo.currentData()
        if fid:
            kwargs["font_id"] = fid
        self.win.apply_edit("set_text_style", id=self._current["id"], **kwargs)

    def _pick_color(self):
        if not self._current:
            return
        c = self._current.get("color") or [0, 0, 0, 1]
        col = QColorDialog.getColor(QColor(int(c[0] * 255), int(c[1] * 255),
                                           int(c[2] * 255)), self, "文字颜色")
        if col.isValid():
            self.win.apply_edit("set_text_style", id=self._current["id"],
                                color=col.name())


def _hline():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


def _label_row(caption: str, value: QLabel) -> QWidget:
    """一行「标签 + 值」，标签固定宽，值占满剩余。"""
    row = QWidget()
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(8)
    lbl = QLabel(caption)
    lbl.setFixedWidth(40)
    lbl.setStyleSheet("color:#6b7280;")
    hl.addWidget(lbl)
    value.setWordWrap(True)
    hl.addWidget(value, 1)
    return row


# ==================================================================== 缩略图

class ThumbList(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.list = QListWidget()
        self.list.setIconSize(QSize(96, 136))
        self.list.setSpacing(4)
        self.list.currentRowChanged.connect(self._on_row)
        lay.addWidget(self.list)
        self._updating = False

    def set_pages(self, pages: List[dict]) -> None:
        self._updating = True
        self.list.clear()
        for p in pages:
            item = QListWidgetItem("第 %d 页  %.0f×%.0f" % (p["index"] + 1, p["w"], p["h"]))
            item.setData(Qt.UserRole, p["index"])
            self.list.addItem(item)
        self._updating = False

    def set_thumb(self, index: int, pix: QPixmap) -> None:
        if 0 <= index < self.list.count():
            self.list.item(index).setIcon(pix)

    def set_current(self, index: int) -> None:
        self._updating = True
        self.list.setCurrentRow(index)
        self._updating = False

    def _on_row(self, row: int):
        if self._updating or row < 0:
            return
        self.win.goto_page(row)


# ==================================================================== 窗口

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.pkg: Optional[OFDPackage] = None
        self.editor: Optional[Editor] = None
        self.page_index = 0
        self.fonts: dict = {}
        self.pending_image: Optional[dict] = None
        self._scene_cache: dict = {}

        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 840)
        self.setAcceptDrops(True)

        self.canvas = PageCanvas()
        self.canvas.window_ref = self
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.canvas)
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignCenter)

        self.thumbs = ThumbList(self)
        self.props = PropsPanel(self)

        split = QSplitter(Qt.Horizontal)
        th = QWidget()
        thl = QVBoxLayout(th)
        thl.setContentsMargins(0, 0, 0, 0)
        thl.addWidget(self.thumbs, 3)
        # 7.8 大纲：页面缩略图下方挂一个树，点击跳页
        self.outline = QTreeWidget()
        self.outline.setHeaderHidden(True)
        self.outline.setIndentation(12)
        self.outline.setMaximumHeight(220)
        self.outline.itemClicked.connect(self._outline_clicked)
        self.outline.hide()
        thl.addWidget(self.outline, 2)
        split.addWidget(th)
        split.addWidget(self.scroll)
        split.addWidget(self.props)
        split.setSizes([170, 800, 280])
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self.setStatusBar(QStatusBar())
        self._status("就绪")
        self._update_actions()

        # 缩略图延后生成，先让窗口出来
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.timeout.connect(self._build_thumbs)

    # ---------------------------------------------------------- 大纲（7.8）
    def _fill_outline(self) -> None:
        """把文档大纲填进树。没有大纲就整块隐藏。"""
        self.outline.clear()
        pkg = getattr(self, "pkg", None)
        if pkg is None:
            self.outline.hide()
            return
        try:
            tree = pkg.document.outlines()
        except Exception:
            tree = []
        if not tree:
            self.outline.hide()
            return
        pages = pkg.all_pages()
        id2idx = {str(p.id): i for i, p in enumerate(pages)}

        def add(parent, nodes):
            for n in nodes:
                it = QTreeWidgetItem([n.get("title") or "(无标题)"])
                act = next((a for a in n.get("actions") or [] if a.get("kind") == "goto"), None)
                if act and act.get("dest") in id2idx:
                    it.setData(0, Qt.UserRole, id2idx[act["dest"]])
                if parent is None:
                    self.outline.addTopLevelItem(it)
                else:
                    parent.addChild(it)
                if n.get("children"):
                    add(it, n["children"])

        add(None, tree)
        self.outline.expandAll()
        self.outline.show()

    def _outline_clicked(self, item, _col) -> None:
        idx = item.data(0, Qt.UserRole)
        if idx is not None:
            self.goto_page(int(idx))

    # ---------------------------------------------------------- UI 构建
    def _build_actions(self):
        self.act_open = QAction("打开…", self, shortcut=QKeySequence.Open,
                                triggered=self.open_file)
        self.act_save = QAction("保存", self, shortcut=QKeySequence.Save,
                                triggered=self.save_file)
        self.act_save_as = QAction("另存为…", self, shortcut="Ctrl+Shift+S",
                                   triggered=self.save_file_as)
        self.act_export_pdf = QAction("导出 PDF…", self, triggered=lambda: self.export("pdf"))
        self.act_export_png = QAction("导出当前页 PNG…", self, triggered=lambda: self.export("png"))
        self.act_export_svg = QAction("导出当前页 SVG…", self, triggered=lambda: self.export("svg"))
        self.act_export_txt = QAction("导出纯文本…", self, triggered=lambda: self.export("txt"))
        self.act_print = QAction("打印…", self, shortcut=QKeySequence.Print,
                                 triggered=self.do_print)
        self.act_quit = QAction("退出", self, shortcut="Ctrl+Q", triggered=self.close)
        self.act_undo = QAction("撤销", self, shortcut=QKeySequence.Undo,
                                triggered=self.undo)
        self.act_redo = QAction("重做", self, shortcut=QKeySequence.Redo,
                                triggered=self.redo)
        self.act_find = QAction("查找替换…", self, shortcut="Ctrl+H",
                                triggered=self.find_replace)
        self.act_text = QAction("插入文字", self, triggered=self.start_place_text)
        self.act_image = QAction("插入图片…", self, triggered=self.start_place_image)
        self.act_rect = QAction("插入方框", self, triggered=self.start_place_rect)
        self.act_delete = QAction("删除选中", self, shortcut=QKeySequence.Delete,
                                  triggered=self.delete_selection)
        self.act_dup = QAction("复制选中", self, shortcut="Ctrl+D",
                               triggered=self.duplicate_selection)
        self.act_zoom_in = QAction("放大", self, shortcut=QKeySequence.ZoomIn,
                                   triggered=lambda: self.set_zoom(self.canvas.zoom * 1.2))
        self.act_zoom_out = QAction("缩小", self, shortcut=QKeySequence.ZoomOut,
                                    triggered=lambda: self.set_zoom(self.canvas.zoom / 1.2))
        self.act_fit = QAction("适应窗口", self, shortcut="Ctrl+0", triggered=self.fit_zoom)
        self.act_rot_page = QAction("顺时针旋转 90°", self, triggered=self.rotate_page)
        self.act_add_page = QAction("在后面插入空白页", self, triggered=self.insert_page)
        self.act_del_page = QAction("删除当前页", self, triggered=self.delete_page)
        self.act_prev = QAction("上一页", self, shortcut="PgUp",
                                triggered=lambda: self.goto_page(self.page_index - 1))
        self.act_next = QAction("下一页", self, shortcut="PgDown",
                                triggered=lambda: self.goto_page(self.page_index + 1))
        self.act_info = QAction("文档信息…", self, triggered=self.show_info)
        self.act_about = QAction("关于", self, triggered=self.show_about)

    def _build_menus(self):
        mb = self.menuBar()
        m = mb.addMenu("文件")
        for a in (self.act_open, self.act_save, self.act_save_as):
            m.addAction(a)
        m.addSeparator()
        for a in (self.act_export_pdf, self.act_export_png, self.act_export_svg,
                  self.act_export_txt):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_print)
        m.addSeparator()
        m.addAction(self.act_quit)

        m = mb.addMenu("编辑")
        for a in (self.act_undo, self.act_redo):
            m.addAction(a)
        m.addSeparator()
        for a in (self.act_find, self.act_text, self.act_image, self.act_rect):
            m.addAction(a)
        m.addSeparator()
        for a in (self.act_delete, self.act_dup):
            m.addAction(a)

        m = mb.addMenu("视图")
        for a in (self.act_zoom_in, self.act_zoom_out, self.act_fit):
            m.addAction(a)

        m = mb.addMenu("页面")
        for a in (self.act_prev, self.act_next, self.act_rot_page,
                  self.act_add_page, self.act_del_page):
            m.addAction(a)

        m = mb.addMenu("帮助")
        m.addAction(self.act_info)
        m.addAction(self.act_about)

    def _build_toolbar(self):
        tb = QToolBar("主工具栏")
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        for a in (self.act_open, self.act_save):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.act_undo, self.act_redo):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.act_text, self.act_image, self.act_rect):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.act_delete, self.act_dup, self.act_find):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.act_print, self.act_export_pdf):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.act_zoom_out, self.act_fit, self.act_zoom_in):
            tb.addAction(a)
        self.addToolBar(tb)

    # ---------------------------------------------------------- 打开/保存
    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开 OFD", "",
                                              "OFD 文档 (*.ofd);;所有文件 (*)")
        if path:
            self.load_document(path)

    def load_document(self, path: str) -> None:
        try:
            pkg = OFDPackage.open(path)
        except OfdError as exc:
            QMessageBox.critical(self, "打开失败", str(exc))
            return
        self.pkg = pkg
        self.editor = Editor(pkg)
        self._scene_cache.clear()
        self.fonts = {}
        for d in pkg.documents:
            for r in d.public_res:
                for fid, f in r.fonts.items():
                    self.fonts[fid] = f.display_name()
        self.page_index = 0
        self.thumbs.set_pages([{"index": i,
                                "w": p.size().w,
                                "h": p.size().h}
                               for i, p in enumerate(pkg.all_pages())])
        self.goto_page(0)
        self._fill_outline()          # 大纲立即填充，不等缩略图的定时器
        self._thumb_timer.start(120)
        self.setWindowTitle("%s — %s" % (os.path.basename(path), APP_TITLE))
        self._status("已打开 %s（%d 页）" % (path, pkg.page_count()))
        # 视图首选项 / 权限 / 签名摘要（第 7.5 与 18 章）
        self._apply_extras(pkg)
        self._update_actions()

    def save_file(self):
        if not self.pkg:
            return
        try:
            self.editor.save()
            self._status("已保存 %s" % self.pkg.path)
            self._update_actions()
        except OfdError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
        # 保存重写了 XML，原签名失效——票据类用途必须让用户知道
        self.statusBar().showMessage(
            "已保存：原始数字签名已失效，仅用于版式调整", 8000)

    def save_file_as(self):
        if not self.pkg:
            return
        path, _ = QFileDialog.getSaveFileName(self, "另存为", self.pkg.path or "未命名.ofd",
                                              "OFD 文档 (*.ofd)")
        if not path:
            return
        try:
            self.editor.save(path)
            self.setWindowTitle("%s — %s" % (os.path.basename(path), APP_TITLE))
            self._status("已另存为 %s" % path)
            self._update_actions()
        except OfdError as exc:
            QMessageBox.critical(self, "另存失败", str(exc))

    # ---------------------------------------------------------- 页面
    def goto_page(self, index: int):
        if not self.pkg:
            return
        pages = self.pkg.all_pages()
        if not pages:
            return
        index = max(0, min(len(pages) - 1, index))
        self.page_index = index
        page = pages[index]
        sc = build_page_scene(page)
        d = sc.to_dict()
        d["index"] = index
        from ofdlite.scene import selection_boxes
        d["objects"] = selection_boxes(page, page.objects())
        self._scene_cache[index] = d
        self.canvas.selection = []
        self.canvas.preview = None
        self.canvas.set_scene(d)
        self.props.set_selection([])
        self.thumbs.set_current(index)
        self.fit_zoom()
        QTimer.singleShot(0, self.fit_zoom)   # 布局完成后再算一次，避免首次用错尺寸
        self._update_actions()
        self.zoom_label_update()

    def refresh_page(self):
        if not self.pkg:
            return
        sel_ids = [o["id"] for o in self.canvas.selection]
        pages = self.pkg.all_pages()
        index = min(self.page_index, len(pages) - 1)
        page = pages[index]
        sc = build_page_scene(page)
        d = sc.to_dict()
        d["index"] = index
        from ofdlite.scene import selection_boxes
        d["objects"] = selection_boxes(page, page.objects())
        self.canvas.set_scene(d)
        self.canvas.selection = [o for o in d["objects"] if o["id"] in sel_ids]
        self.props.set_selection(self.canvas.selection)
        self.canvas.update()
        self.thumbs.set_thumb(index, self._thumb_for(page))
        self._update_actions()

    def _thumb_for(self, page) -> QPixmap:
        sc = build_page_scene(page).to_dict()
        img = render_page_to_image(sc, zoom=1.6)
        return QPixmap.fromImage(img)

    def _build_thumbs(self):
        if not self.pkg:
            return
        for i, page in enumerate(self.pkg.all_pages()):
            self.thumbs.set_thumb(i, self._thumb_for(page))

    def set_zoom(self, zoom: float):
        self.canvas.set_zoom(zoom)
        self.zoom_label_update()

    def fit_zoom(self, keep_if_zoomed=True):
        if not self.canvas.scene:
            return
        w, h = self.canvas.page_size()
        # 预留：画布自身 48px 内边距 + 滚动条宽度，避免"出现滚动条→视口变窄→
        # 再出现滚动条"的循环
        sb = self.scroll.verticalScrollBar().sizeHint().width() or 16
        avail_w = max(200, self.scroll.viewport().width() - 48 - sb)
        avail_h = max(200, self.scroll.viewport().height() - 48 - sb)
        zoom = min(avail_w / max(w, 1e-6), avail_h / max(h, 1e-6))
        self.canvas.set_zoom(max(0.6, min(20.0, zoom)))
        self.zoom_label_update()

    def zoom_label_update(self):
        pct = self.canvas.zoom / 3.7795 * 100
        self.statusBar().showMessage(
            "%s · 第 %d/%d 页 · 缩放 %d%%" % (
                os.path.basename(self.pkg.path if self.pkg else "") or "未打开",
                self.page_index + 1,
                self.pkg.page_count() if self.pkg else 0,
                int(pct)))

    # ---------------------------------------------------------- 编辑回调
    def on_selection_changed(self, sel):
        self.props.set_selection(sel)
        self._update_actions()

    def apply_edit(self, op: str, **kwargs):
        if not self.editor or not self.pkg:
            return
        page_obj = None
        try:
            page = self.pkg.all_pages()[self.page_index]
            if op == "set_text":
                obj = self._find_obj(kwargs["id"])
                if obj is not None:
                    self.editor.set_text(obj, kwargs.get("text") or "")
            elif op == "set_text_style":
                obj = self._find_obj(kwargs["id"])
                if obj is not None:
                    color = kwargs.get("color")
                    rgba = None
                    if color:
                        from ofdlite import colors as colorlib
                        rgba = colorlib.parse_color(color)
                    self.editor.set_text_style(
                        obj, size_mm=kwargs.get("size"), bold=kwargs.get("bold"),
                        italic=kwargs.get("italic"), color=rgba,
                        font_id=kwargs.get("font_id"),
                        spacing=kwargs.get("spacing"))
            elif op == "move":
                objs = [self._find_obj(i) for i in kwargs.get("ids") or []]
                objs = [o for o in objs if o is not None]
                if objs:
                    self.editor.move(objs, kwargs.get("dx") or 0, kwargs.get("dy") or 0)
            elif op == "delete":
                objs = [self._find_obj(i) for i in kwargs.get("ids") or []]
                objs = [o for o in objs if o is not None]
                if objs:
                    self.editor.delete_objects(objs)
            elif op == "duplicate":
                objs = [self._find_obj(i) for i in kwargs.get("ids") or []]
                objs = [o for o in objs if o is not None]
                if objs:
                    self.editor.duplicate_objects(objs)
            del page_obj
        except (OfdError, EditError) as exc:
            QMessageBox.warning(self, "操作失败", str(exc))
            return
        self.refresh_page()

    def _find_obj(self, obj_id: str):
        if not self.pkg:
            return None
        page = self.pkg.all_pages()[self.page_index]
        for o in page.objects():
            if o.id == str(obj_id):
                return o
        return None

    def move_selection(self, dx: float, dy: float):
        ids = [o["id"] for o in self.canvas.selection]
        if ids:
            self.apply_edit("move", ids=ids, dx=dx, dy=dy)

    def delete_selection(self):
        ids = [o["id"] for o in self.canvas.selection]
        if not ids:
            self._status("没有选中对象")
            return
        self.apply_edit("delete", ids=ids)

    def duplicate_selection(self):
        ids = [o["id"] for o in self.canvas.selection]
        if not ids:
            return
        self.apply_edit("duplicate", ids=ids)

    def undo(self):
        if not self.editor or not self.editor.can_undo():
            return
        label = self.editor.undo()
        if self.pkg.page_count() != self.thumbs.list.count():
            self.thumbs.set_pages([{"index": i, "w": p.size().w, "h": p.size().h}
                                   for i, p in enumerate(self.pkg.all_pages())])
            self._thumb_timer.start(120)
        self.goto_page(self.page_index)
        self._status("已撤销：%s" % (label or ""))

    def redo(self):
        if not self.editor or not self.editor.can_redo():
            return
        label = self.editor.redo()
        self.goto_page(self.page_index)
        self._status("已重做：%s" % (label or ""))

    # ---------------------------------------------------------- 插入
    def start_place_text(self):
        if not self.pkg:
            return
        self.canvas.mode = "place-text"
        self._status("在页面上点一下，放置新文字")

    def start_place_rect(self):
        if not self.pkg:
            return
        self.canvas.mode = "place-rect"
        self._status("在页面上拖一个方框")

    def start_place_image(self):
        if not self.pkg:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择图片", "",
                                              "图片 (*.png *.jpg *.jpeg *.bmp *.gif)")
        if not path:
            return
        with open(path, "rb") as fh:
            self.pending_image = {"name": os.path.basename(path), "data": fh.read()}
        self.canvas.mode = "place-image"
        self._status("在页面上点一下，放置图片 %s" % os.path.basename(path))

    def add_text_at(self, x: float, y: float):
        self.canvas.mode = "select"
        page = self.pkg.all_pages()[self.page_index]
        try:
            self.editor.add_text(page, x, y, "新文本", size_mm=5.0,
                                 font_id=self._default_font_id())
        except OfdError as exc:
            QMessageBox.warning(self, "插入失败", str(exc))
            return
        self.refresh_page()
        self._status("已插入文字")

    def add_rect_at(self, x0: float, y0: float, x1: float, y1: float):
        self.canvas.mode = "select"
        page = self.pkg.all_pages()[self.page_index]
        # 直接用页面绝对坐标写路径：add_path 会把 bbox 当 Boundary，
        # 一次调用就落位，不再额外 move（否则撤销要按两次）
        xa, ya = min(x0, x1), min(y0, y1)
        xb, yb = max(x0, x1), max(y0, y1)
        d = "M %s %s L %s %s L %s %s L %s %s Z" % (
            geo._fmt(xa), geo._fmt(ya), geo._fmt(xb), geo._fmt(ya),
            geo._fmt(xb), geo._fmt(yb), geo._fmt(xa), geo._fmt(yb))
        try:
            self.editor.add_path(page, [d], stroke=(0.2, 0.2, 0.2, 1),
                                 width_mm=0.4)
        except OfdError as exc:
            QMessageBox.warning(self, "插入失败", str(exc))
            return
        self.refresh_page()

    def add_image_at(self, x: float, y: float):
        self.canvas.mode = "select"
        info = self.pending_image or {}
        self.pending_image = None
        if not info:
            return
        page = self.pkg.all_pages()[self.page_index]
        w, h = 40.0, 30.0
        try:
            self.editor.add_image(page, info["data"], info["name"],
                                  x - w / 2, y - h / 2, w, h)
        except OfdError as exc:
            QMessageBox.warning(self, "插入失败", str(exc))
            return
        self.refresh_page()

    def _default_font_id(self) -> str:
        for fid in sorted(self.fonts.keys()):
            return fid
        return "1"

    # ---------------------------------------------------------- 页面操作
    def rotate_page(self):
        if not self.pkg:
            return
        page = self.pkg.all_pages()[self.page_index]
        try:
            self.editor.set_page_rotation(page, (page.rotation() + 90) % 360)
        except OfdError as exc:
            QMessageBox.warning(self, "旋转失败", str(exc))
            return
        self.goto_page(self.page_index)

    def insert_page(self):
        if not self.pkg:
            return
        try:
            self.editor.insert_page(self.page_index + 1)
        except OfdError as exc:
            QMessageBox.warning(self, "插页失败", str(exc))
            return
        self.thumbs.set_pages([{"index": i, "w": p.size().w, "h": p.size().h}
                               for i, p in enumerate(self.pkg.all_pages())])
        self.goto_page(self.page_index + 1)
        self._thumb_timer.start(120)

    def delete_page(self):
        if not self.pkg:
            return
        try:
            self.editor.delete_page(self.page_index)
        except (OfdError, EditError) as exc:
            QMessageBox.warning(self, "删页失败", str(exc))
            return
        self.thumbs.set_pages([{"index": i, "w": p.size().w, "h": p.size().h}
                               for i, p in enumerate(self.pkg.all_pages())])
        self.goto_page(min(self.page_index, self.pkg.page_count() - 1))
        self._thumb_timer.start(120)

    # ---------------------------------------------------------- 查找
    def find_replace(self):
        if not self.pkg:
            return
        find, ok = QInputDialog.getText(self, "查找替换", "查找：")
        if not ok or not find:
            return
        repl, ok = QInputDialog.getText(self, "查找替换", "替换为：")
        if not ok:
            return
        n = self.editor.replace_all(find, repl)
        self.refresh_page()
        QMessageBox.information(self, "查找替换", "共替换 %d 处" % n)

    # ---------------------------------------------------------- 文本弹窗
    def edit_text_dialog(self, obj_info: dict):
        obj = self._find_obj(obj_info["id"])
        if obj is None or not isinstance(obj, model.TextObject):
            return
        dlg = TextEditDialog(obj, self.fonts, self)
        if dlg.exec() == QDialog.Accepted:
            v = dlg.values()
            try:
                if v["text"] != obj.text():
                    self.editor.set_text(obj, v["text"])
                self.editor.set_text_style(obj, size_mm=v["size"], bold=v["bold"],
                                           italic=v["italic"], color=v["color"],
                                           font_id=v.get("font_id") or None)
            except OfdError as exc:
                QMessageBox.warning(self, "修改失败", str(exc))
                return
            self.refresh_page()

    # ---------------------------------------------------------- 导出/打印
    def export(self, fmt: str):
        if not self.pkg:
            return
        if self._permissions().get("Export") is False:
            QMessageBox.warning(self, "不能导出",
                                "文档权限声明禁止导出（表8 Export=false）。")
            return
        from ofdlite import convert
        if fmt == "pdf":
            path, _ = QFileDialog.getSaveFileName(
                self, "导出 PDF", os.path.splitext(self.pkg.path or "out")[0] + ".pdf",
                "PDF (*.pdf)")
            if not path:
                return
            try:
                convert.export_pdf(self.pkg, path, dpi=200)
            except OfdError as exc:
                QMessageBox.critical(self, "导出失败", str(exc))
                return
            self._status("已导出 PDF：%s" % path)
            return
        if fmt == "txt":
            path, _ = QFileDialog.getSaveFileName(
                self, "导出文本", os.path.splitext(self.pkg.path or "out")[0] + ".txt",
                "文本 (*.txt)")
            if not path:
                return
            convert.export_text(self.pkg, path)
            self._status("已导出文本：%s" % path)
            return
        page = self.pkg.all_pages()[self.page_index]
        if fmt == "png":
            path, _ = QFileDialog.getSaveFileName(self, "导出 PNG", "page.png", "PNG (*.png)")
            if not path:
                return
            sc = build_page_scene(page).to_dict()
            img = render_page_to_image(sc, zoom=300 / 25.4)
            img.save(path)
            self._status("已导出 PNG：%s" % path)
            return
        if fmt == "svg":
            path, _ = QFileDialog.getSaveFileName(self, "导出 SVG", "page.svg", "SVG (*.svg)")
            if not path:
                return
            convert.export_svg(page, path)
            self._status("已导出 SVG：%s" % path)

    def do_print(self):
        if not self.pkg:
            return
        from ofdlite import extras as _extras
        perm = self._permissions()
        limit = _extras.printable_count(perm)
        if limit == 0:
            QMessageBox.warning(self, "不能打印",
                                "文档权限声明禁止打印（表8 Printable=false 或 Copies=0）。")
            return
        from .print_dialog import PrintDialog
        dlg = PrintDialog(self.pkg, self.page_index, self,
                          copies_limit=limit)
        dlg.exec()

    # ---------------------------------------------------------- 杂项
    def show_info(self):
        if not self.pkg:
            return
        from ofdlite.convert import structure_report
        rep = structure_report(self.pkg)
        lines = ["文件：%s" % rep["path"],
                 "包内条目：%d" % rep["entries"],
                 "文档数：%d   总页数：%d" % (rep["documents"], rep["pages"]), ""]
        for d in rep["docs"]:
            lines.append("标题：%s" % (d["title"] or "—"))
            lines.append("作者：%s" % (d["author"] or "—"))
            lines.append("字体：%s" % (", ".join("%s=%s" % kv for kv in d["fonts"].items()) or "无"))
            lines.append("图片：%s" % (", ".join("%s=%s" % kv for kv in d["images"].items()) or "无"))
            for p in d["pages"]:
                lines.append("  第 %d 页 %sx%s mm 对象 %d %s" % (
                    p["index"] + 1, p["size"][0], p["size"][1], p["objects"],
                    " ".join("%s%d" % kv for kv in p["kinds"].items())))
        QMessageBox.information(self, "文档信息", "\n".join(lines))

    def show_about(self):
        from ofdlite import __version__
        QMessageBox.about(self, "关于", (
            "<b>小黄OFD %s</b><br>"
            "BUILD_ID %s<br>"
            "Copyright (c) 2026 小黄 &lt;15359927763&gt; QQ 1924053793<br>"
            "阅读、编辑、打印 OFD 文档。<br>"
            "核心库 ofdlite 纯 Python 实现，桌面端与网页端共用。<br>"
            "Qt 绑定：%s" % (__version__, __import__("ofdlite").build_id(), QT_API)))

    def _update_actions(self):
        has_doc = self.pkg is not None
        for a in (self.act_save, self.act_save_as, self.act_export_pdf,
                  self.act_export_png, self.act_export_svg, self.act_export_txt,
                  self.act_print, self.act_text, self.act_image, self.act_rect,
                  self.act_find, self.act_rot_page, self.act_add_page, self.act_del_page):
            a.setEnabled(has_doc)
        # 表8 权限声明：文件自己说不许做什么，菜单项就置灰并写清原因
        perm = self._permissions()
        if has_doc:
            from ofdlite import extras as _ex
            period = _ex.validity_status(perm)
            if period in ("expired", "not-started"):
                msg = ("文档已过访问有效期（表8 ValidPeriod）" if period == "expired"
                       else "文档访问有效期尚未开始（表8 ValidPeriod）")
                for a in (self.act_save, self.act_save_as, self.act_export_pdf,
                          self.act_export_png, self.act_export_svg,
                          self.act_export_txt, self.act_print):
                    a.setEnabled(False)
                    a.setToolTip(msg)
        if has_doc and perm.get("Edit") is False:
            for a in (self.act_save, self.act_save_as):
                a.setEnabled(False)
                a.setToolTip("文档权限声明禁止编辑（表8 Edit=false）")
        if has_doc and perm.get("Export") is False:
            for a in (self.act_export_pdf, self.act_export_png,
                      self.act_export_svg, self.act_export_txt):
                a.setEnabled(False)
                a.setToolTip("文档权限声明禁止导出（表8 Export=false）")
        if has_doc and perm.get("Printable") is False:
            self.act_print.setEnabled(False)
            self.act_print.setToolTip("文档权限声明禁止打印（表8 Printable=false）")
        self.act_undo.setEnabled(has_doc and self.editor.can_undo())
        self.act_redo.setEnabled(has_doc and self.editor.can_redo())
        sel = bool(self.canvas.selection)
        self.act_delete.setEnabled(has_doc and sel)
        self.act_dup.setEnabled(has_doc and sel)

    def _permissions(self) -> dict:
        if not self.pkg or not self.pkg.documents:
            return {}
        return self.pkg.document.permissions or {}

    def _apply_extras(self, pkg) -> None:
        """把第 7.5 / 18 章的东西落到界面上：视图首选项、权限、签名、DocType。"""
        doc = pkg.document
        pref = doc.vpreferences or {}
        if pref.get("HideToolbar"):
            for tb in self.findChildren(QToolBar):
                tb.setVisible(False)
        if pref.get("HideMenubar"):
            self.menuBar().setVisible(False)
        if pref.get("Zoom"):
            try:
                self.set_zoom(float(pref["Zoom"]) / 100.0)
            except Exception:
                pass
        sig = (doc.signatures or {}).get("items") or []
        bits = ["DocType=%s" % (pkg.doc_type or "-")]
        bits.append("签名 %d 个" % len(sig) if sig else "无签名")
        if (doc.permissions or {}).get("Export") is False:
            bits.append("禁导出")
        if (doc.permissions or {}).get("Printable") is False:
            bits.append("禁打印")
        self._status(" · ".join(bits))

    def _status(self, text: str):
        self.statusBar().showMessage(text)

    # ---------------------------------------------------------- 拖放
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            for u in ev.mimeData().urls():
                if u.toLocalFile().lower().endswith(".ofd"):
                    ev.acceptProposedAction()
                    return

    def dropEvent(self, ev):
        for u in ev.mimeData().urls():
            p = u.toLocalFile()
            if p.lower().endswith(".ofd"):
                self.load_document(p)
                break

    def closeEvent(self, ev):
        if self.editor and self.editor.dirty:
            r = QMessageBox.question(
                self, "未保存的改动", "文档有未保存的改动，要保存吗？",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                ev.ignore()
                return
            if r == QMessageBox.Save:
                try:
                    self.editor.save()
                except OfdError as exc:
                    QMessageBox.critical(self, "保存失败", str(exc))
                    ev.ignore()
                    return
        ev.accept()


# ==================================================================== 文本弹窗

class TextEditDialog(QDialog):
    def __init__(self, obj, fonts: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑文字")
        self.resize(460, 340)
        lay = QVBoxLayout(self)
        self.edit = QLineEdit(obj.text())
        lay.addWidget(QLabel("文字内容"))
        lay.addWidget(self.edit)

        form = QFormLayout()
        self.size = QDoubleSpinBox()
        self.size.setRange(0.5, 300)
        self.size.setSingleStep(0.5)
        self.size.setSuffix(" mm")
        self.size.setValue(obj.size_mm())
        form.addRow("字号", self.size)

        self.bold = QCheckBox("加粗")
        self.bold.setChecked(obj.bold())
        self.italic = QCheckBox("斜体")
        self.italic.setChecked(obj.italic())
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.bold)
        hl.addWidget(self.italic)
        hl.addStretch(1)
        form.addRow("字形", row)

        self.font = QComboBox()
        self.font.addItem("（保持）", "")
        for fid, name in fonts.items():
            self.font.addItem("%s (#%s)" % (name, fid), str(fid))
        idx = self.font.findData(str(obj.font_id()))
        self.font.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("字体", self.font)

        self.color_btn = QPushButton("选择颜色…")
        c = obj.fill_color() or (0, 0, 0, 1)
        self._color = c
        self.color_btn.setStyleSheet("background:#%02x%02x%02x" % (
            int(c[0] * 255), int(c[1] * 255), int(c[2] * 255)))
        self.color_btn.clicked.connect(self._pick)
        form.addRow("颜色", self.color_btn)

        lay.addLayout(form)
        lay.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _pick(self):
        c = self._color
        col = QColorDialog.getColor(QColor(int(c[0] * 255), int(c[1] * 255),
                                           int(c[2] * 255)), self, "文字颜色")
        if col.isValid():
            self._color = (col.redF(), col.greenF(), col.blueF(), 1.0)
            self.color_btn.setStyleSheet("background:%s" % col.name())

    def values(self) -> dict:
        return {
            "text": self.edit.text(),
            "size": self.size.value(),
            "bold": self.bold.isChecked(),
            "italic": self.italic.isChecked(),
            "color": self._color,
            "font_id": self.font.currentData(),
        }

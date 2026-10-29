# -*- coding: utf-8 -*-
"""打印对话框。

两条输出路径：
  1. CUPS 直发（推荐）：拼版 → PDF → lp。能带份数、双面、纸张、N 合 1。
  2. Qt 打印：QPrinter 直接打当前文档，矢量，走系统打印对话框。

页范围、份数、N 合 1、双面、小册子在两条路径里都支持。
"""
from __future__ import annotations

import os
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from ofdlite import model
from ofdlite import print_linux
from ofdlite.scene import build_page_scene


def parse_ranges(text: str, total: int):
    if not text or not text.strip():
        return list(range(total))
    out = []
    for part in text.split(","):
        s = part.strip()
        if not s:
            continue
        if "-" in s:
            a, b = s.split("-", 1)
            try:
                a, b = int(a), int(b)
            except ValueError:
                continue
            for i in range(min(a, b), max(a, b) + 1):
                if 1 <= i <= total:
                    out.append(i - 1)
        elif s.isdigit():
            i = int(s)
            if 1 <= i <= total:
                out.append(i - 1)
    return sorted(set(out))


class PrintDialog(QDialog):
    def __init__(self, pkg: model.OFDPackage, page_index: int = 0, parent=None):
        super().__init__(parent)
        self.pkg = pkg
        self.page_index = page_index
        self.setWindowTitle("打印")
        self.resize(520, 460)
        self._result_pdf = None

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.printer = QComboBox()
        self.printer.addItem("（系统默认）", "")
        self._printers = []
        for p in print_linux.list_printers():
            self._printers.append(p)
            self.printer.addItem("%s%s" % (p["name"], (" — " + p["info"][:36]) if p["info"] else ""),
                                 p["name"])
        dflt = print_linux.default_printer()
        if dflt:
            idx = self.printer.findData(dflt)
            if idx >= 0:
                self.printer.setCurrentIndex(idx)
        form.addRow("打印机", self.printer)

        self.pages = QLineEdit()
        self.pages.setPlaceholderText("留空 = 全部；示例 1-3,5")
        form.addRow("页码范围", self.pages)

        self.copies = QSpinBox()
        self.copies.setRange(1, 99)
        self.copies.setValue(1)
        form.addRow("份数", self.copies)

        self.nup = QComboBox()
        for v, t in ((1, "1 合 1"), (2, "2 合 1"), (4, "4 合 1"),
                     (6, "6 合 1"), (9, "9 合 1")):
            self.nup.addItem(t, v)
        form.addRow("每张页数", self.nup)

        self.paper = QComboBox()
        for p in ("A4", "A3", "A5", "B5", "16K", "Letter", "Legal"):
            self.paper.addItem(p, p)
        form.addRow("纸张", self.paper)

        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        self.duplex = QCheckBox("双面（长边翻页）")
        self.booklet = QCheckBox("小册子（骑马钉）")
        hl.addWidget(self.duplex)
        hl.addWidget(self.booklet)
        hl.addStretch(1)
        form.addRow("版式", row)

        self.dpi = QSpinBox()
        self.dpi.setRange(72, 600)
        self.dpi.setSingleStep(24)
        self.dpi.setValue(200)
        form.addRow("渲染 dpi", self.dpi)

        lay.addLayout(form)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#6b7280;")
        lay.addWidget(self.hint)
        if not print_linux.cups_available():
            self.hint.setText("系统里没有 lp（cups-client 未安装）。可以先生成 PDF，"
                              "再用任意阅读器打印。")
        elif not self._printers:
            self.hint.setText("CUPS 里没有配置打印机。请先在系统设置里添加，"
                              "或点「生成 PDF」。")

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        lay.addWidget(self.log)

        btns = QHBoxLayout()
        self.btn_pdf = QPushButton("生成 PDF…")
        self.btn_pdf.clicked.connect(self.export_pdf)
        self.btn_qt = QPushButton("用 Qt 打印…")
        self.btn_qt.clicked.connect(self.print_with_qt)
        self.btn_cups = QPushButton("打印")
        self.btn_cups.setDefault(True)
        self.btn_cups.clicked.connect(self.print_with_cups)
        btns.addWidget(self.btn_pdf)
        btns.addWidget(self.btn_qt)
        btns.addStretch(1)
        cancel = QPushButton("关闭")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        btns.addWidget(self.btn_cups)
        lay.addLayout(btns)

    # ---------------------------------------------------------- 参数
    def selected_pages(self):
        return parse_ranges(self.pages.text(), self.pkg.page_count())

    def _log(self, text: str):
        self.log.appendPlainText(text)

    # ---------------------------------------------------------- CUPS
    def print_with_cups(self):
        pages = self.selected_pages()
        if not pages:
            QMessageBox.warning(self, "打印", "页码范围无效")
            return
        if not print_linux.cups_available():
            QMessageBox.warning(self, "打印", "系统里没有 CUPS 客户端（lp），"
                                             "请先安装 cups-client，或改用「生成 PDF」。")
            return
        try:
            res = print_linux.print_document(
                self.pkg,
                printer=self.printer.currentData() or None,
                pages=pages,
                copies=self.copies.value(),
                n_up=self.nup.currentData(),
                paper=self.paper.currentData(),
                duplex=self.duplex.isChecked(),
                booklet=self.booklet.isChecked(),
                dpi=self.dpi.value(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "打印失败", str(exc))
            return
        ok = res.get("ok")
        self._log("$ %s" % res.get("cmd"))
        if res.get("stdout"):
            self._log(res["stdout"])
        if res.get("stderr"):
            self._log("stderr: " + res["stderr"])
        self._log("PDF: %s" % res.get("pdf"))
        if ok:
            QMessageBox.information(self, "打印", "任务已提交。\n%s" % (res.get("stdout") or ""))
        else:
            QMessageBox.warning(self, "打印失败",
                                "lp 返回 %s\n%s\n\n生成的 PDF 在：\n%s"
                                % (res.get("code"), res.get("stderr"), res.get("pdf")))

    # ---------------------------------------------------------- Qt 打印
    def print_with_qt(self):
        pages = self.selected_pages()
        if not pages:
            QMessageBox.warning(self, "打印", "页码范围无效")
            return
        printer = QPrinter(QPrinter.HighResolution)
        printer.setDocName(self.pkg.document.title or "OFD")
        printer.setPageSize(QPageSize(_qpage_size(self.paper.currentData())))
        printer.setPageOrientation(QPageLayout.Portrait)
        if self.duplex.isChecked():
            printer.setDuplex(QPrinter.DuplexLongSide)
        printer.setCopyCount(self.copies.value())
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("选择打印机")
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            n = _paint_document(printer, self.pkg, pages, self.nup.currentData(),
                                self.booklet.isChecked())
            self._log("Qt 打印完成，输出 %d 个页面" % n)
            QMessageBox.information(self, "打印", "已提交给打印机。")
        except Exception as exc:
            QMessageBox.critical(self, "打印失败", str(exc))

    # ---------------------------------------------------------- PDF
    def export_pdf(self):
        pages = self.selected_pages()
        if not pages:
            QMessageBox.warning(self, "导出", "页码范围无效")
            return
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, "生成打印用 PDF",
                                              "print.pdf", "PDF (*.pdf)")
        if not path:
            return
        try:
            from ofdlite.convert import export_pdf
            export_pdf(self.pkg, path, dpi=self.dpi.value(), pages=pages)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self._result_pdf = path
        self._log("已生成 %s（%d 字节）" % (path, os.path.getsize(path)))
        QMessageBox.information(self, "导出", "已生成：\n%s" % path)


def _qpage_size(name: str):
    mapping = {
        "A4": QPageSize.A4, "A3": QPageSize.A3, "A5": QPageSize.A5,
        "B5": QPageSize.B5, "Letter": QPageSize.Letter, "Legal": QPageSize.Legal,
    }
    return mapping.get(name, QPageSize.A4)


def _device_rect(device):
    """从 QPrinter 或 QPdfWriter 取可打印区域（设备像素）与 dpi。"""
    if hasattr(device, "pageRect"):
        r = device.pageRect(QPrinter.DevicePixel)
        return r, max(72.0, float(device.resolution()))
    if hasattr(device, "pageLayout"):
        layout = device.pageLayout()
        res = float(device.resolution())
        r = layout.paintRectPixels(int(res))
        return r, max(72.0, res)
    # 兜底：按 A4 / 96dpi 估
    from PySide6.QtCore import QRect
    return QRect(0, 0, 794, 1123), 96.0


def _paint_document(printer, pkg: model.OFDPackage, pages, n_up: int = 1,
                    booklet: bool = False) -> int:
    """把文档画到 QPrinter 或 QPdfWriter。矢量输出，N 合 1 在这里排。"""
    from PySide6.QtGui import QPainter
    from ofdlite.print_linux import booklet_order
    from app.qt_renderer import QtSceneRenderer

    all_pages = pkg.all_pages()
    renderer = QtSceneRenderer()
    painter = QPainter()
    if not painter.begin(printer):
        raise RuntimeError("无法开始打印（目标设备不可用）")

    rect, dpi = _device_rect(printer)
    zoom = dpi / 25.4

    seq = list(pages)
    if booklet:
        # booklet_order 会补空位到 4 的倍数，返回里可能带 None，这里映射回页号
        idx = []
        for i in booklet_order(len(seq)):
            idx.append(seq[i] if (i is not None and 0 <= i < len(seq)) else None)
    else:
        idx = list(seq)

    per_sheet = max(1, int(n_up))
    grid = {1: (1, 1), 2: (2, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3)}.get(per_sheet, (1, 1))
    cols, rows = grid

    margin = 8.0 * zoom
    gap = 4.0 * zoom
    cell_w = (rect.width() - 2 * margin - gap * (cols - 1)) / cols
    cell_h = (rect.height() - 2 * margin - gap * (rows - 1)) / rows

    printed = 0
    first = True
    for i in range(0, len(idx), per_sheet):
        chunk = idx[i:i + per_sheet]
        if not first:
            printer.newPage()
        first = False
        for k, pidx in enumerate(chunk):
            if pidx is None or not (0 <= pidx < len(all_pages)):
                continue
            page = all_pages[pidx]
            sc = build_page_scene(page).to_dict()
            size = sc.get("size") or [210, 297]
            r, c = divmod(k, cols)
            cx = rect.x() + margin + c * (cell_w + gap)
            cy = rect.y() + margin + r * (cell_h + gap)
            k_scale = min(cell_w / (size[0] * zoom), cell_h / (size[1] * zoom))
            painter.save()
            painter.translate(cx + (cell_w - size[0] * zoom * k_scale) / 2,
                              cy + (cell_h - size[1] * zoom * k_scale) / 2)
            painter.scale(k_scale, k_scale)
            renderer.render(painter, sc, zoom, clip_outline=False)
            painter.restore()
            printed += 1
    painter.end()
    return printed

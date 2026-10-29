# -*- coding: utf-8 -*-
"""Qt 绘制后端：把 scene 的绘制指令画到 QPainter 上。

与 Pillow 后端、浏览器 Canvas 后端共用同一份指令协议，所以三处显示一致。
单位 mm，QPainter 上先做 zoom 缩放（scale），之后全部用毫米坐标画。
"""
from __future__ import annotations

import base64
from typing import Dict, List, Optional

try:
    from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
    from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage,
                               QPainter, QPainterPath, QPen, QPixmap,
                               QPolygonF, QTransform)
    QT_API = "PySide6"
except ImportError:  # 麒麟上常见 PySide2 / PyQt5
    try:
        from PySide2.QtCore import QByteArray, QPointF, QRectF, Qt
        from PySide2.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage,
                                   QPainter, QPainterPath, QPen, QPixmap,
                                   QPolygonF, QTransform)
        QT_API = "PySide2"
    except ImportError:
        from PyQt5.QtCore import QByteArray, QPointF, QRectF, Qt
        from PyQt5.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage,
                                 QPainter, QPainterPath, QPen, QPixmap,
                                 QPolygonF, QTransform)
        QT_API = "PyQt5"

_ALIGN_LEFT = Qt.AlignLeft | Qt.AlignBottom


def qcolor(rgba) -> QColor:
    if not rgba:
        return QColor(0, 0, 0)
    r = int(round(rgba[0] * 255))
    g = int(round(rgba[1] * 255))
    b = int(round(rgba[2] * 255))
    a = int(round((rgba[3] if len(rgba) > 3 else 1.0) * 255))
    return QColor(r, g, b, a)


class QtSceneRenderer(object):
    """一次创建，多次绘制。图片解码结果会缓存。"""

    def __init__(self):
        self._pixmaps: Dict[str, QPixmap] = {}
        self._images_pending: Dict[str, dict] = {}
        self.warnings: List[str] = []

    # ---------------------------------------------------------- 资源
    def load_resources(self, resources: dict) -> None:
        for key, meta in (resources or {}).items():
            if key in self._pixmaps:
                continue
            data = meta.get("data") or ""
            if not data:
                self._images_pending[key] = meta
                continue
            try:
                raw = QByteArray(base64.b64decode(data))
                pm = QPixmap()
                if pm.loadFromData(raw):
                    self._pixmaps[key] = pm
                else:
                    self.warnings.append("图片 %s 无法解码" % key)
            except Exception as exc:
                self.warnings.append("图片 %s 解码异常: %s" % (key, exc))

    # ---------------------------------------------------------- 绘制
    def render(self, painter: QPainter, scene: dict, zoom: float,
               preview: Optional[dict] = None, clip_outline: bool = True) -> None:
        """zoom = 像素/毫米。scene 是 build_page_scene 的 to_dict()。"""
        size = scene.get("size") or [210, 297]
        self.load_resources(scene.get("resources") or {})
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.scale(zoom, zoom)
        if clip_outline:
            painter.setClipRect(QRectF(0, 0, size[0], size[1]))
        painter.fillRect(QRectF(0, 0, size[0], size[1]), QColor(255, 255, 255))

        ops = scene.get("ops") or []
        for op in ops:
            shift = None
            if preview and op.get("id") and op.get("id") in preview.get("ids", ()):
                shift = (preview["dx"], preview["dy"])
            if shift:
                painter.save()
                painter.translate(shift[0], shift[1])
            try:
                t = op.get("t")
                if t == "rect":
                    self._rect(painter, op)
                elif t == "path":
                    self._path(painter, op)
                elif t == "text":
                    self._text(painter, op)
                elif t == "image":
                    self._image(painter, op)
            except Exception as exc:
                self.warnings.append("绘制 %s 失败: %s" % (op.get("t"), exc))
            finally:
                if shift:
                    painter.restore()
        painter.restore()

    def _rect(self, painter: QPainter, op: dict) -> None:
        fill = qcolor(op.get("fill"))
        stroke = op.get("stroke")
        if op.get("fill"):
            painter.fillRect(QRectF(op["x"], op["y"], op["w"], op["h"]), fill)
        if stroke:
            pen = QPen(qcolor(stroke))
            pen.setWidthF(0.1)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(QRectF(op["x"], op["y"], op["w"], op["h"]))

    def _path(self, painter: QPainter, op: dict) -> None:
        path = QPainterPath()
        subs = op.get("subs") or []
        closed = op.get("closed") or []
        if not subs:
            return
        for i, arr in enumerate(subs):
            if len(arr) < 2:
                continue
            path.moveTo(arr[0], arr[1])
            for k in range(2, len(arr) - 1, 2):
                path.lineTo(arr[k], arr[k + 1])
            if i < len(closed) and closed[i]:
                path.closeSubpath()
        if op.get("fill"):
            path.setFillRule(Qt.WindingFill if op.get("rule") != "evenodd" else Qt.OddEvenFill)
            painter.fillPath(path, QBrush(qcolor(op["fill"])))
        if op.get("stroke"):
            pen = QPen(qcolor(op["stroke"]))
            pen.setWidthF(max(0.02, float(op.get("w") or 0.2)))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            dash = op.get("dash")
            if dash:
                # Qt 的虚线单位是笔宽，换算成毫米
                w = pen.widthF() or 0.2
                pen.setDashPattern([max(0.1, d / w) for d in dash])
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

    def _text(self, painter: QPainter, op: dict) -> None:
        txt = op.get("text") or ""
        if not txt:
            return
        size = float(op.get("size") or 3.0)
        font = QFont(op.get("font") or "Noto Sans CJK SC")
        font.setPixelSize(max(1, int(round(size * 2.834645))))  # 先给个默认，避免 0
        # 直接设 pointSizeF 会受 DPI 影响；这里用 pixelSize 配合 painter 的 scale
        font.setPixelSize(max(1, int(round(size))))
        font.setBold(bool(op.get("bold")))
        font.setItalic(bool(op.get("italic")))
        if op.get("spacing"):
            font.setLetterSpacing(QFont.AbsoluteSpacing, float(op["spacing"]))
        painter.setFont(font)
        painter.setPen(QPen(qcolor(op.get("color") or [0, 0, 0, 1])))
        rot = float(op.get("rot") or 0.0)
        # HScale（表45）：字宽的水平缩放比，0.5 表示字宽减半
        hscale = float(op.get("hscale") or 1.0)
        if abs(rot) > 0.01 or abs(hscale - 1.0) > 0.001:
            painter.save()
            painter.translate(op["x"], op["y"])
            if abs(rot) > 0.01:
                painter.rotate(rot)
            if abs(hscale - 1.0) > 0.001:
                painter.scale(hscale, 1.0)
            painter.drawText(QPointF(0, 0), txt)
            painter.restore()
        else:
            painter.drawText(QPointF(op["x"], op["y"]), txt)

    def _image(self, painter: QPainter, op: dict) -> None:
        pm = self._pixmaps.get(op.get("res"))
        if pm is None:
            return
        corners = op.get("corners") or []
        painter.save()
        if float(op.get("alpha") or 1.0) < 1.0:
            painter.setOpacity(float(op.get("alpha") or 1.0))
        if len(corners) == 4:
            p0, p1, _p2, p3 = corners
            w = op.get("w") or 1.0
            h = op.get("h") or 1.0
            a = (p1[0] - p0[0]) / w
            b = (p1[1] - p0[1]) / w
            c = (p3[0] - p0[0]) / h
            d = (p3[1] - p0[1]) / h
            tr = QTransform(a, b, c, d, p0[0], p0[1])
            painter.setTransform(tr, True)
            painter.drawPixmap(QRectF(0, 0, w, h), pm, QRectF(pm.rect()))
        else:
            painter.drawPixmap(QRectF(op["x"], op["y"], op["w"], op["h"]), pm,
                               QRectF(pm.rect()))
        painter.restore()

    # ---------------------------------------------------------- 辅助
    def text_width_mm(self, text: str, font_family: str, size_mm: float,
                      bold: bool = False, italic: bool = False) -> float:
        """用 Qt 的字体度量算文本宽度（毫米），给自动换行/边界用。"""
        font = QFont(font_family or "Noto Sans CJK SC")
        font.setPixelSize(max(1, int(round(size_mm))))
        font.setBold(bold)
        font.setItalic(italic)
        fm = QFontMetricsF(font)
        return fm.horizontalAdvance(text)


def render_page_to_image(scene: dict, zoom: float = 3.78,
                         background="#ffffff") -> QImage:
    """离屏渲染一页成 QImage（用于缩略图 / 导出 / 无界面截图）。"""
    size = scene.get("size") or [210, 297]
    w = max(1, int(round(size[0] * zoom)))
    h = max(1, int(round(size[1] * zoom)))
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor(background))
    p = QPainter(img)
    r = QtSceneRenderer()
    r.render(p, scene, zoom, clip_outline=False)
    p.end()
    return img


def render_page_to_pdf_pages(pdf_writer, scenes: List[dict], dpi: float = 150.0) -> None:
    """把一个或多个场景写进 QPdfWriter（矢量）。调用方负责 newPage。"""
    r = QtSceneRenderer()
    for i, scene in enumerate(scenes):
        if i:
            pdf_writer.newPage()
        p = QPainter(pdf_writer)
        size = scene.get("size") or [210, 297]
        target = pdf_writer.pageLayout().paintRectPixels(int(dpi)) if hasattr(
            pdf_writer, "pageLayout") else None
        zoom = dpi / 25.4
        if target is not None:
            avail_w = target.width() / zoom
            avail_h = target.height() / zoom
            k = min(avail_w / max(size[0], 1e-6), avail_h / max(size[1], 1e-6))
            if k < 1:
                p.translate((avail_w - size[0] * k) / 2 * zoom,
                            (avail_h - size[1] * k) / 2 * zoom)
                p.scale(k, k)
        r.render(p, scene, zoom, clip_outline=False)
        p.end()

# -*- coding: utf-8 -*-
"""桌面端入口。

    python -m app.main [文件.ofd]
    python app/main.py [文件.ofd]

环境变量：
    OFD_FONT_DIRS   追加字体目录（冒号分隔）
    QT_QPA_PLATFORM offscreen 可无界面运行（跑自测/截图时用）
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QIcon, QPalette, QColor
    from PySide6.QtWidgets import QApplication
    QT_MAJOR = 6
except ImportError:
    try:
        from PySide2.QtCore import Qt
        from PySide2.QtGui import QFont, QIcon, QPalette, QColor
        from PySide2.QtWidgets import QApplication
        QT_MAJOR = 2
    except ImportError:
        from PyQt5.QtCore import Qt
        from PyQt5.QtGui import QFont, QIcon, QPalette, QColor
        from PyQt5.QtWidgets import QApplication
        QT_MAJOR = 5

from .mainwindow import MainWindow, APP_TITLE


def build_stylesheet() -> str:
    """麒麟/UKUI 风格的浅色主题：扁平、1px 细边、蓝色主色。"""
    return """
    QMainWindow, QDialog { background: #f5f6f8; }
    QMenuBar { background: #fafbfc; border-bottom: 1px solid #d8dade; }
    QMenuBar::item { padding: 5px 11px; background: transparent; }
    QMenuBar::item:selected { background: #e8f3fc; }
    QMenu { background: #ffffff; border: 1px solid #d8dade; padding: 4px; }
    QMenu::item { padding: 5px 24px 5px 20px; }
    QMenu::item:selected { background: #e8f3fc; }
    QToolBar { background: #fafbfc; border-bottom: 1px solid #d8dade;
               spacing: 4px; padding: 4px 8px; }
    QToolButton { padding: 5px 11px; border: 1px solid transparent;
                  border-radius: 4px; }
    QToolButton:hover { background: #eef5fb; border-color: #cfe0ef; }
    QToolButton:pressed { background: #e0eef9; }
    QToolButton:disabled { color: #a8adb4; }
    QStatusBar { background: #ffffff; border-top: 1px solid #d8dade; color: #5c6169; }
    QScrollArea { border: none; background: #e9ebee; }
    QListWidget { border: none; background: #ffffff; }
    QListWidget::item { padding: 4px; border: 1px solid transparent; }
    QListWidget::item:selected { background: #e8f3fc; border-color: #0072c6;
                                 color: #1f2328; }
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {
        background: #ffffff; border: 1px solid #cfd3d9; border-radius: 4px;
        padding: 4px 6px; selection-background-color: #bcdcf5; }
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
        border-color: #0072c6; }
    QPushButton { background: #ffffff; border: 1px solid #cfd3d9;
                  border-radius: 4px; padding: 6px 14px; }
    QPushButton:hover { background: #f2f7fb; border-color: #b9c4d0; }
    QPushButton:pressed { background: #e6eef6; }
    QPushButton:default { background: #0072c6; color: #ffffff; border-color: #0072c6; }
    QPushButton:default:hover { background: #0064b0; }
    QPushButton:disabled { color: #a8adb4; background: #f4f5f7; }
    QLabel { color: #1f2328; }
    QSplitter::handle { background: #d8dade; width: 1px; }
    """


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("OFD Lite")
    app.setStyleSheet(build_stylesheet())

    font = QFont()
    # 麒麟默认字体优先，找不到就按 fontconfig 的匹配走
    for fam in ("Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei",
                "Microsoft YaHei", "PingFang SC"):
        font.setFamily(fam)
        break
    font.setPointSize(10)
    app.setFont(font)

    win = MainWindow()
    args = [a for a in argv[1:] if not a.startswith("-")]
    if args and os.path.exists(args[0]):
        win.load_document(os.path.abspath(args[0]))
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

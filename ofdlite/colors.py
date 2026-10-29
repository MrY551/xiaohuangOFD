# -*- coding: utf-8 -*-
"""颜色解析。

OFD 里颜色的表达方式很杂，实际文件里见过：
    FillColor="#FF0000"                    -> 十六进制
    FillColor="255 0 0" ColorSpace="1"     -> DeviceRGB 十进制
    FillColor="128" ColorSpace="DeviceGray"-> 灰度
    FillColor="0 0 0 0" ColorSpace="DeviceCMYK"
    FillColor="255,0,0"                    -> 逗号分隔
Alpha 是 0~255，255 表示完全不透明。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

RGBA = Tuple[float, float, float, float]  # 0..1

# 常见的 ColorSpace 取值约定（GB/T 33190 里 ColorSpace 是 ID 引用，
# 但不少工具直接写固定名或固定编号，两种都兜住）
_STD_SPACES = {
    "1": "rgb", "devicergb": "rgb", "rgb": "rgb",
    "2": "cmyk", "devicecmyk": "cmyk", "cmyk": "cmyk",
    "3": "gray", "devicegray": "gray", "gray": "gray", "grey": "gray",
    "4": "gray", "5": "rgb", "8": "cmyk",
}

NAMED = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 128, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "cyan": (0, 255, 255), "magenta": (255, 0, 255), "gray": (128, 128, 128),
    "grey": (128, 128, 128),
}


def _norm_space(cs) -> str:
    if cs is None:
        return "rgb"
    s = str(cs).strip()
    if s in _STD_SPACES:
        return _STD_SPACES[s]
    return "rgb"


def parse_color(value, space=None, alpha=None, default=None) -> Optional[RGBA]:
    """把 (value, space, alpha) 转成 RGBA 0..1。解析不出返回 default。"""
    if value is None or value == "":
        return default
    s = str(value).strip()

    rgb = None
    if s.startswith("#"):
        h = s[1:].strip()
        try:
            if len(h) == 3:
                r, g, b = (int(c * 2, 16) for c in h)
            elif len(h) == 6:
                r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            elif len(h) == 8:
                r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:4 + 2], 16)
            else:
                return default
            rgb = (r, g, b)
        except ValueError:
            return default
    elif s.lower() in NAMED:
        rgb = NAMED[s.lower()]
    elif re.match(r"^\d+\s+\d+\s+\d+(\s+\d+)?$", s) or "," in s:
        parts = [p for p in re.split(r"[,\s]+", s) if p != ""]
        try:
            vals = [float(p) for p in parts]
        except ValueError:
            return default
        sp = _norm_space(space)
        if sp == "cmyk" and len(vals) >= 4:
            c, m, y, k = [v / 255.0 if v > 1 else v for v in vals[:4]]
            r = 255 * (1 - c) * (1 - k)
            g = 255 * (1 - m) * (1 - k)
            b = 255 * (1 - y) * (1 - k)
            rgb = (r, g, b)
        elif sp == "gray" and len(vals) >= 1:
            v = vals[0]
            v = v if v <= 1 else v / 255.0
            rgb = (v * 255, v * 255, v * 255)
        elif len(vals) >= 3:
            rgb = tuple(vals[:3])
        elif len(vals) == 1:
            v = vals[0]
            v = v if v <= 1 else v / 255.0
            rgb = (v * 255, v * 255, v * 255)
        else:
            return default
    elif re.match(r"^-?\d+(\.\d+)?$", s):
        try:
            v = float(s)
        except ValueError:
            return default
        v = v if v <= 1 else v / 255.0
        rgb = (v * 255, v * 255, v * 255)
    else:
        return default

    r, g, b = [max(0.0, min(255.0, float(c))) for c in rgb]
    a = 1.0
    if alpha is not None and alpha != "":
        try:
            av = float(alpha)
            a = av if av <= 1.0 else av / 255.0
            a = max(0.0, min(1.0, a))
        except ValueError:
            a = 1.0
    return (r / 255.0, g / 255.0, b / 255.0, a)


def to_hex(rgba: RGBA) -> str:
    r, g, b, _ = rgba
    return "#%02X%02X%02X" % (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def to_ofd_value(rgba: RGBA, space: str = "rgb") -> Tuple[str, str, str]:
    """反向：RGBA -> (ColorSpace, ColorValue, Alpha)。编辑时写回用。"""
    r, g, b, a = rgba
    if space == "gray":
        v = int(round((0.299 * r + 0.587 * g + 0.114 * b) * 255))
        return ("DeviceGray", str(v), str(int(round(a * 255))))
    if space == "cmyk":
        k = 1.0 - max(r, g, b)
        if k >= 1.0:
            return ("DeviceCMYK", "0 0 0 255", str(int(round(a * 255))))
        c = (1 - r - k) / (1 - k)
        m = (1 - g - k) / (1 - k)
        y = (1 - b - k) / (1 - k)
        return ("DeviceCMYK",
                " ".join(str(int(round(v * 255))) for v in (c, m, y, k)),
                str(int(round(a * 255))))
    return ("DeviceRGB",
            " ".join(str(int(round(v * 255))) for v in (r, g, b)),
            str(int(round(a * 255))))

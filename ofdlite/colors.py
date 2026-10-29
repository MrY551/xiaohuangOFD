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
    low = s.lower()
    if low in _STD_SPACES:
        return _STD_SPACES[low]
    if s in _STD_SPACES:
        return _STD_SPACES[s]
    return "rgb"


def channel_scale(bpc) -> float:
    """表27：通道取值范围是 [0, 2^BPC-1]，BPC 默认 8。"""
    try:
        n = int(bpc)
    except (TypeError, ValueError):
        n = 8
    if n < 1 or n > 16:
        n = 8
    return float((1 << n) - 1)


def parse_color(value, space=None, alpha=None, default=None,
                bpc=8, palette=None, index=None) -> Optional[RGBA]:
    """把 (value, space, alpha) 转成 RGBA 0..1。解析不出返回 default。

    参数按标准第 8.3 章：
      space   颜色空间类型，取 GRAY / RGB / CMYK（也接受资源 ID 解析后的类型名）
      bpc     表25 BitsPerComponent（1/2/4/8/16，默认 8），决定通道上限 2^BPC-1
      palette 表25 调色板（CV 列表），配合 index 使用
      index   表26 的 Index，Value 不出现时按调色板取色

    通道三个写法都要认（表27 的示例就是这么写的）：
      "#11 #22 #33"  逐通道十六进制
      "#FF0000"      整串十六进制（工具届惯例，标准没写但真实文件常见）
      "173 51 68"    十进制
    """
    s = "" if value is None else str(value).strip()
    kind = _norm_space(space)
    ch = None                                   # 未归一化的通道值

    # 表26：Value 不出现时用 Index 从调色板取值
    if not s:
        try:
            ix = int(index) if index not in (None, "") else None
        except (TypeError, ValueError):
            ix = None
        if ix is None or not palette or ix < 0 or ix >= len(palette):
            return default
        ch = [float(v) for v in palette[ix]]
        if not ch:
            return default
    elif s.startswith("#"):
        toks = s.split()
        if len(toks) >= 2 and all(t.startswith("#") for t in toks):
            try:
                ch = [int(t[1:], 16) for t in toks]
            except ValueError:
                return default
        else:
            h = s[1:].strip()
            try:
                if len(h) == 1:                 # "#F" → 单通道满值
                    ch = [int(h * 2, 16)]
                elif len(h) == 2:               # 表27 的 GRAY 写法 "#FF"
                    ch = [int(h, 16)]
                elif len(h) == 3:
                    rgb = tuple(int(c * 2, 16) for c in h)
                elif len(h) == 6:
                    rgb = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
                elif len(h) == 8:
                    rgb = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
                else:
                    return default
            except ValueError:
                return default
            if ch is None:
                ch = list(rgb)
    elif s.lower() in NAMED:
        ch = list(NAMED[s.lower()])
    elif re.match(r"^\d+\s+\d+\s+\d+(\s+\d+)?$", s) or "," in s:
        parts = [p for p in re.split(r"[,\s]+", s) if p != ""]
        try:
            ch = [float(p) for p in parts]
        except ValueError:
            return default
    elif re.match(r"^-?\d+(\.\d+)?$", s):
        try:
            ch = [float(s)]
        except ValueError:
            return default
    else:
        return default

    # 通道数决定颜色空间（表27：Gray 1 个、RGB 3 个、CMYK 4 个）。
    # 标准若同时给了 Type，按 Type；Type 与通道数打架时以通道数为准。
    if len(ch) == 1:
        kind = "gray"
    elif len(ch) == 4:
        kind = "cmyk"
    elif len(ch) == 3:
        kind = "rgb"

    scale = channel_scale(bpc)
    # 0..1 的小数写法是工具届惯例（标准要求整数）。只在 BPC=8、全部 ≤1 时采用，
    # 与旧行为一致，避免把真实的 0/1 通道值误判。
    fractional = scale == 255.0 and all(v <= 1.0 for v in ch)

    def unit(v):
        if fractional:
            return float(v)
        return float(v) / scale

    # 表27：通道值超出 [0, 2^BPC-1] 时按默认颜色处理
    if not fractional:
        for v in ch:
            if v < 0 or v > scale:
                return default

    if kind == "cmyk":
        if len(ch) < 4:
            return default
        c, m, y, k = [unit(v) for v in ch[:4]]
        rgb = (255 * (1 - c) * (1 - k),
               255 * (1 - m) * (1 - k),
               255 * (1 - y) * (1 - k))
    elif kind == "gray":
        v = unit(ch[0])
        rgb = (v * 255, v * 255, v * 255)
    else:
        if len(ch) < 3:
            return default
        rgb = tuple(unit(v) * 255.0 for v in ch[:3])

    r, g, b = [max(0.0, min(255.0, float(c))) for c in rgb]

    # 表26：Alpha 是 0~255 的整数，255 完全不透明（BPC 不参与）。
    # 带小数点的写法按 0..1 处理，兼容工具惯用写法。
    a = 1.0
    if alpha is not None and alpha != "":
        try:
            raw = str(alpha).strip()
            av = float(raw)
            if "." in raw:
                a = av
            else:
                a = av / 255.0
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

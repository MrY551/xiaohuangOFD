# -*- coding: utf-8 -*-
"""Rust 加速内核的加载与软降级（GB/T 33190-2016 的图形渲染热点）。

设计原则：**能加载 .so 就用，加载不到就回落纯 Python**。

这样三件事同时成立：
  · 有预编译内核时，光栅化快 70 倍；
  · 目标机上没有 .so（或架构不匹配）时，功能一点不少，只是慢；
  · 测试永远能用纯 Python 路径跑，不依赖编译产物。

接口与 ofdlite/raster.fill_coverage 保持一致，调用方不需要知道下面是谁。
"""
from __future__ import annotations

import ctypes
import os
from typing import List, Sequence, Tuple

AVAILABLE = False
VERSION = ""
_REASON = ""
_lib = None

# 按优先级找内核：包内预编译 → 源码树的构建产物
_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.path.join(_HERE, "rustcore", "libofd_core.so"),          # 打包时落在这里
    os.path.join(_HERE, "rustcore", "target", "release", "libofd_core.so"),
    os.path.join(_HERE, "rustcore", "target", "release", "libofd_core.dylib"),
    os.path.join(_HERE, "rustcore", "target", "release", "ofd_core.dll"),
]


def _try_load():
    global _lib, AVAILABLE, VERSION, _REASON
    if _lib is not None:
        return _lib
    # 诊断/对比用：OFD_NO_RUST=1 强制走纯 Python。
    # 也方便在出问题时一键排除内核的因素。
    if os.environ.get("OFD_NO_RUST", "").strip() not in ("", "0"):
        _REASON = "已被 OFD_NO_RUST 禁用"
        return None
    for path in _CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            lib = ctypes.CDLL(path)
        except OSError as exc:
            _REASON = "%s 加载失败: %s" % (os.path.basename(path), exc)
            continue

        # ---- 绑定签名（零拷贝：缓冲由 Python 侧给）
        lib.ofd_fill_coverage.restype = None
        lib.ofd_fill_coverage.argtypes = [
            ctypes.POINTER(ctypes.c_double), ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        lib.ofd_blend.restype = None
        lib.ofd_blend.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte,
        ]
        lib.ofd_fill_coverage_multi.restype = None
        lib.ofd_fill_coverage_multi.argtypes = [
            ctypes.POINTER(ctypes.c_int), ctypes.c_int,
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        # JPEG 解码的三个热点
        lib.ofd_ycbcr_to_rgb.restype = None
        lib.ofd_ycbcr_to_rgb.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int, ctypes.c_int,          # w, h
            ctypes.c_int, ctypes.c_int,          # yw, yh
            ctypes.c_int, ctypes.c_int,          # cw, ch
            ctypes.c_int, ctypes.c_int,          # sh, sv
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        lib.ofd_gray_to_rgb.restype = None
        lib.ofd_gray_to_rgb.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        lib.ofd_idct8x8.restype = None
        lib.ofd_idct8x8.argtypes = [
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ushort),
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        lib.ofd_blend_rect.restype = None
        lib.ofd_blend_rect.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int,
            ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte,
        ]
        lib.ofd_core_version.restype = ctypes.c_char_p
        lib.ofd_core_version.argtypes = []

        try:
            VERSION = (lib.ofd_core_version() or b"").decode("ascii", "replace")
        except Exception:
            VERSION = "unknown"

        _lib = lib
        AVAILABLE = True
        _REASON = ""
        return lib
    return None


def status() -> Tuple[bool, str]:
    """返回 (是否可用, 说明)。给 CLI --version / 诊断用。"""
    _try_load()
    if AVAILABLE:
        return True, "Rust 内核 %s" % VERSION
    return False, (_REASON or "未找到 libofd_core.so，使用纯 Python 路径")


def fill_coverage(polys: Sequence[Sequence[Tuple[float, float]]],
                  width: int, height: int,
                  rule: str = "nonzero", ss: int = 4):
    """扫描线填充，返回覆盖率（bytearray，长度 width*height）。

    polys 是「子路径」的列表；这里按**并集**处理（逐个填、取最大覆盖率），
    与 raster.fill_coverage 对多子路径的语义一致。
    """
    lib = _try_load()
    if lib is None:
        from .raster import fill_coverage as _py
        return _py(polys, width, height, rule, ss)

    w, h = int(width), int(height)
    if w <= 0 or h <= 0:
        return bytearray(0)

    out = bytearray(w * h)
    dst = (ctypes.c_ubyte * (w * h)).from_buffer(out)
    evenodd = 1 if str(rule).replace("-", "").replace("_", "").lower() in (
        "evenodd") else 0

    # 收集有效子路径的点数与坐标
    counts = []
    coords = []
    for poly in polys:
        n = len(poly)
        if n < 3:
            continue
        counts.append(n)
        for (x, y) in poly:
            coords.append(float(x))
            coords.append(float(y))
    if not counts:
        return out

    # 多子路径回落纯 Python。
    #
    # 两个四边形的简单用例里 Rust 与纯 Python 逐字节一致（同向嵌套 10000=10000），
    # 但**真实页面解禁后四页 sha 全变**——说明还有边界情况没覆盖（自相交、
    # 退化边、极短子路径等）。正确性优先，暂时只让单子路径走 Rust ——
    # 单子路径是绝大多数情况，那部分已逐字节验证。
    if len(counts) > 1:
        from .raster import fill_coverage as _py
        return _py(polys, w, h, rule, ss)

    carr = (ctypes.c_int * len(counts))(*counts)
    farr = (ctypes.c_double * len(coords))(*coords)
    lib.ofd_fill_coverage_multi(carr, len(counts), farr, w, h,
                                evenodd, int(ss), dst)
    return out


def blend(buf, cov, width: int, height: int, rgba) -> bool:
    """把覆盖率按颜色混进 RGBA 画布。

    buf 必须是可写的 bytearray（长度 width*height*4）；cov 是覆盖率。
    返回 True 表示走了 Rust，False 表示调用方应自己走 Python 路径。
    """
    lib = _try_load()
    if lib is None:
        return False
    w, h = int(width), int(height)
    n = w * h
    if w <= 0 or h <= 0 or len(buf) < n * 4 or len(cov) < n:
        return False
    try:
        bsrc = (ctypes.c_ubyte * (n * 4)).from_buffer(buf)
        csrc = (ctypes.c_ubyte * n).from_buffer_copy(cov)
    except Exception:
        return False
    r, g, b, a = (int(rgba[0]), int(rgba[1]), int(rgba[2]), int(rgba[3]))
    lib.ofd_blend(csrc, w, h, bsrc, r, g, b, a)
    return True


# ---------------------------------------------------------------- JPEG 帮手

def ycbcr_to_rgb(yd, cd, crd, w: int, h: int, yw: int, yh: int,
                 cw: int, ch: int, sh: int, sv: int):
    """YCbCr → 交错 RGB。返回 bytes 或 None（None 表示调用方该走 Python 路径）。

    `yw/yh` 是 **Y 平面的行宽与行数**（= bw*8, bh*8），不是图像宽高 ——
    平面按 8x8 块存、块数按 MCU 对齐，所以 yw 可能大于 w。
    """
    lib = _try_load()
    if lib is None:
        return None
    n = w * h
    if n <= 0 or yw <= 0 or yh <= 0 or cw <= 0 or ch <= 0 or sh <= 0 or sv <= 0:
        return None
    if len(yd) < yw * yh or len(cd) < cw * ch or len(crd) < cw * ch:
        return None
    ya = (ctypes.c_ubyte * (yw * yh)).from_buffer_copy(yd)
    ca = (ctypes.c_ubyte * (cw * ch)).from_buffer_copy(cd)
    ra = (ctypes.c_ubyte * (cw * ch)).from_buffer_copy(crd)
    out = bytearray(n * 3)
    oa = (ctypes.c_ubyte * (n * 3)).from_buffer(out)
    lib.ofd_ycbcr_to_rgb(ya, ca, ra, w, h, yw, yh, cw, ch, sh, sv, oa)
    return bytes(out)


def gray_to_rgb(yd, n: int):
    """单分量展开成 RGB。返回 bytes 或 None。暂时禁用，原因同上。"""
    return None
    lib = _try_load()
    if lib is None or n <= 0 or len(yd) < n:
        return None
    ya = (ctypes.c_ubyte * n).from_buffer_copy(yd)
    out = bytearray(n * 3)
    oa = (ctypes.c_ubyte * (n * 3)).from_buffer(out)
    lib.ofd_gray_to_rgb(ya, n, oa)
    return bytes(out)


def idct8x8(coef, qt):
    """反量化 + IDCT，返回 64 个像素（bytes）或 None。

    `coef` 是**已经反量化过**的自然序系数，所以 `qt` 传全 1 ——
    Rust 侧按标准公式会再乘一次量化表，传全 1 才不会乘两遍。
    """
    lib = _try_load()
    if lib is None or len(coef) < 64 or len(qt) < 64:
        return None
    ca = (ctypes.c_int * 64)(*[int(v) for v in coef[:64]])
    qa = (ctypes.c_ushort * 64)(*[int(v) for v in qt[:64]])
    out = bytearray(64)
    oa = (ctypes.c_ubyte * 64).from_buffer(out)
    lib.ofd_idct8x8(ca, qa, oa)
    return bytes(out)


def blend_rect(buf, cov, mw: int, mh: int, bw: int, bh: int,
               ox: int, oy: int, rgba) -> bool:
    """把 mw*mh 的覆盖率按颜色混进画布的 (ox, oy) 处。

    buf 是可写 bytearray（bw*bh*4）；cov 是覆盖率（mw*mh）。
    返回 True 表示走了 Rust，False 表示调用方应自己走 Python 路径。
    """
    lib = _try_load()
    if lib is None:
        return False
    if mw <= 0 or mh <= 0 or bw <= 0 or bh <= 0 or len(cov) < mw * mh:
        return False
    if len(buf) < bw * bh * 4:
        return False
    try:
        ca = (ctypes.c_ubyte * (mw * mh)).from_buffer_copy(cov)
        ba = (ctypes.c_ubyte * (bw * bh * 4)).from_buffer(buf)
    except Exception:
        return False
    lib.ofd_blend_rect(ca, mw, mh, ba, bw, bh, ox, oy,
                       int(rgba[0]), int(rgba[1]), int(rgba[2]), int(rgba[3]))
    return True

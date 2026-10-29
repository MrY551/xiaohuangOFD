# -*- coding: utf-8 -*-
"""定位 WPS 第 3 页在 Rust 内核与纯 Python 之间的差异来源。

逐个关掉 Rust 的三个入口，看哪一个关掉之后 sha 与纯 Python 一致。
"""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofdlite import OFDPackage, _fast  # noqa: E402
from ofdlite.render_soft import render_page  # noqa: E402

WPS = "/mnt/c/Users/19240/WPSDrive/245473181/WPS云盘/市场运营部 2026年第三季度工作总结.ofd"
if not os.path.exists(WPS):
    WPS = "C:/Users/19240/WPSDrive/245473181/WPS云盘/市场运营部 2026年第三季度工作总结.ofd"

PAGE = 2  # 第 3 页（0 起算）


def sha_of(pg):
    cv, _ = render_page(pg, 96)
    return hashlib.sha256(bytes(cv.buf)).hexdigest()[:8]


def main():
    pkg = OFDPackage.open(WPS)
    pg = pkg.all_pages()[PAGE]

    print("内核:", _fast.status()[1])
    print()

    # 基线：全开
    base = sha_of(pg)
    print("全开                 %s" % base)

    # 逐个禁用
    orig_blend = _fast.blend_rect
    orig_fill = _fast.fill_coverage
    orig_rgb = _fast.ycbcr_to_rgb
    orig_idct = _fast.idct8x8

    try:
        _fast.blend_rect = lambda *a, **k: False
        print("禁 blend_rect        %s" % sha_of(pg))
        _fast.blend_rect = orig_blend

        _fast.fill_coverage = lambda polys, w, h, rule="nonzero", ss=4: \
            orig_fill.__globals__["_py_fill_coverage"](polys, w, h, rule, ss) \
            if "_py_fill_coverage" in orig_fill.__globals__ else \
            __import__("ofdlite.raster", fromlist=["fill_coverage"]).fill_coverage(
                polys, w, h, rule, ss)
        print("禁 fill_coverage     %s" % sha_of(pg))
        _fast.fill_coverage = orig_fill

        _fast.ycbcr_to_rgb = lambda *a, **k: None
        print("禁 ycbcr_to_rgb      %s" % sha_of(pg))
        _fast.ycbcr_to_rgb = orig_rgb

        _fast.idct8x8 = lambda *a, **k: None
        print("禁 idct8x8           %s" % sha_of(pg))
        _fast.idct8x8 = orig_idct
    finally:
        _fast.blend_rect = orig_blend
        _fast.fill_coverage = orig_fill
        _fast.ycbcr_to_rgb = orig_rgb
        _fast.idct8x8 = orig_idct

    # 纯 Python 基线
    _fast._lib = None
    _fast.AVAILABLE = False
    os.environ["OFD_NO_RUST"] = "1"
    pure = sha_of(pg)
    print()
    print("纯 Python 基线       %s" % pure)


if __name__ == "__main__":
    main()

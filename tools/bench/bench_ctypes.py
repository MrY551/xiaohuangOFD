# -*- coding: utf-8 -*-
"""验证：Python 通过 ctypes 调 C 内核，端到端能快多少。

对比两件事：
  1. 单次 fill_coverage 的耗时（含 ctypes 调用开销）
  2. 用真实发票整页渲染，只把 fill_coverage 换成 C 版，看整页时间的差
"""
import ctypes
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofdlite.raster import fill_coverage as py_fill  # noqa: E402

SO = os.path.join(HERE, "libraster_c.so")
if not os.path.exists(SO):
    print("先编译: gcc -O3 -shared -fPIC -o libraster_c.so raster_c.c -lm")
    sys.exit(1)

lib = ctypes.CDLL(SO)
lib.ofd_fill_coverage.restype = ctypes.POINTER(ctypes.c_ubyte)
lib.ofd_fill_coverage.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
]
libc = ctypes.CDLL(None)
libc.free.argtypes = [ctypes.c_void_p]

# 零拷贝接口：直接写进 Python 的 bytearray，不复制 100 万字节
lib.ofd_fill_coverage_into.restype = None
lib.ofd_fill_coverage_into.argtypes = [
    ctypes.POINTER(ctypes.c_double), ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.POINTER(ctypes.c_ubyte),
]


def c_fill(polys, w, h, evenodd, ss):
    """把 py 的多边形转成 C 的扁平坐标，调一次，返回 bytes。"""
    npts = sum(len(p) for p in polys)
    arr = (ctypes.c_double * (npts * 2))()
    k = 0
    for poly in polys:
        for x, y in poly:
            arr[k] = x
            arr[k + 1] = y
            k += 2
    ptr = lib.ofd_fill_coverage(arr, npts, w, h, 1 if evenodd else 0, ss)
    if not ptr:
        return None
    try:
        return bytes(ptr[: w * h])
    finally:
        libc.free(ctypes.cast(ptr, ctypes.c_void_p))


def c_fill_into(polys, w, h, evenodd, ss, buf):
    """零拷贝：把覆盖率写进调用方给的 bytearray。"""
    npts = sum(len(p) for p in polys)
    arr = (ctypes.c_double * (npts * 2))()
    k = 0
    for poly in polys:
        for x, y in poly:
            arr[k] = x
            arr[k + 1] = y
            k += 2
    dst = (ctypes.c_ubyte * (w * h)).from_buffer(buf)
    lib.ofd_fill_coverage_into(arr, npts, w, h, 1 if evenodd else 0, ss, dst)
    return buf


def bench_single():
    W, H, SS, N = 1240, 827, 4, 720
    cx, cy = W / 2.0, H / 2.0
    r = min(W, H) * 0.45
    poly = [(cx + r * math.cos(2 * math.pi * i / N),
             cy + r * math.sin(2 * math.pi * i / N)) for i in range(N)]
    polys = [poly]

    py_fill(polys, W, H, "nonzero", SS)
    t0 = time.perf_counter()
    for _ in range(2):
        a = py_fill(polys, W, H, "nonzero", SS)
    t1 = time.perf_counter()
    py_ms = (t1 - t0) * 1000 / 2

    c_fill(polys, W, H, False, SS)
    t0 = time.perf_counter()
    for _ in range(40):
        b = c_fill(polys, W, H, False, SS)
    t1 = time.perf_counter()
    c_ms = (t1 - t0) * 1000 / 40

    buf = bytearray(W * H)
    c_fill_into(polys, W, H, False, SS, buf)
    t0 = time.perf_counter()
    for _ in range(40):
        c_fill_into(polys, W, H, False, SS, buf)
    t1 = time.perf_counter()
    into_ms = (t1 - t0) * 1000 / 40

    same = (bytes(a) == bytes(b))
    same_into = (bytes(a) == bytes(buf))
    print("单次 fill_coverage（%dx%d, ss=%d, %d 段圆）" % (W, H, SS, N))
    print("  Python       %.3f ms" % py_ms)
    print("  Python→C     %.3f ms   返回时复制 100 万字节" % c_ms)
    print("  Python→C 零拷贝 %.3f ms   写进调用方缓冲" % into_ms)
    print("  加速(复制)   %.1f×" % (py_ms / c_ms))
    print("  加速(零拷贝) %.1f×" % (py_ms / into_ms))
    print("  结果一致:    %s / %s" % ("是" if same else "否", "是" if same_into else "否"))
    print()
    return py_ms / c_ms


def bench_page():
    """真实发票整页渲染：只把 raster.fill_coverage 换成 C 版。"""
    p = "/mnt/c/Users/19240/OneDrive/文档/数电发票25357000000165684636.ofd"
    if not os.path.exists(p):
        p = "C:/Users/19240/OneDrive/文档/数电发票25357000000165684636.ofd"
    if not os.path.exists(p):
        print("真实发票不在，跳过整页对比")
        return
    from ofdlite import OFDPackage
    from ofdlite.render_soft import render_page
    import ofdlite.render_soft as RS

    pkg = OFDPackage.open(p)
    pg = pkg.all_pages()[0]

    t0 = time.perf_counter()
    cv1, w1 = render_page(pg, 150)
    t1 = time.perf_counter()
    py_ms = (t1 - t0) * 1000

    orig = RS.fill_coverage

    real_w = ctypes.CDLL(SO)

    def patched(polys, w, h, rule, ss=4):
        # 用零拷贝接口，跟单次基准里验证过的写法一致
        npts = sum(len(p) for p in polys)
        arr = (ctypes.c_double * (npts * 2))()
        k = 0
        for poly in polys:
            for x, y in poly:
                arr[k] = x
                arr[k + 1] = y
                k += 2
        out = (ctypes.c_ubyte * (w * h))()
        real_w.ofd_fill_coverage_into(arr, npts, w, h,
                                      1 if rule == "evenodd" else 0, ss, out)
        return out

    RS.fill_coverage = patched
    try:
        t0 = time.perf_counter()
        cv2, w2 = render_page(pg, 150)
        t1 = time.perf_counter()
        c_ms = (t1 - t0) * 1000
    finally:
        RS.fill_coverage = orig

    nz1 = sum(1 for k in range(0, len(cv1.buf), 4)
              if not (cv1.buf[k] > 250 and cv1.buf[k+1] > 250 and cv1.buf[k+2] > 250))
    nz2 = sum(1 for k in range(0, len(cv2.buf), 4)
              if not (cv2.buf[k] > 250 and cv2.buf[k+1] > 250 and cv2.buf[k+2] > 250))
    print("真实发票整页渲染（210x140mm @150dpi）")
    print("  纯 Python     %.1f ms   非白 %d" % (py_ms, nz1))
    print("  编译器内核     %.1f ms   非白 %d" % (c_ms, nz2))
    print("  整页加速       %.2f×" % (py_ms / c_ms))
    print("  非白差异       %d" % (nz2 - nz1))
    print("  告警          %s" % (w1 or ["无"][0] if not w2 else w2[:1]))


if __name__ == "__main__":
    bench_single()
    bench_page()

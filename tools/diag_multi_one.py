# -*- coding: utf-8 -*-
"""用 out/multi_probe.py 的多边形单点复现，把覆盖率打成矩阵找规律。"""
import ctypes
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "out") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "out"))

from ofdlite import _fast  # noqa: E402
from ofdlite.raster import fill_coverage as py_fill  # noqa: E402

from multi_probe import POLYS, W, H, RULE, SS  # noqa: E402


def rust_multi(polys, w, h, rule, ss):
    _fast._lib = None
    _fast.AVAILABLE = False
    lib = _fast._try_load()
    counts, coords = [], []
    for poly in polys:
        if len(poly) < 3:
            continue
        counts.append(len(poly))
        for (x, y) in poly:
            coords.append(float(x))
            coords.append(float(y))
    out = bytearray(w * h)
    dst = (ctypes.c_ubyte * (w * h)).from_buffer(out)
    carr = (ctypes.c_int * len(counts))(*counts)
    farr = (ctypes.c_double * len(coords))(*coords)
    evenodd = 1 if str(rule).replace("-", "").replace("_", "").lower() == "evenodd" else 0
    lib.ofd_fill_coverage_multi(carr, len(counts), farr, w, h, evenodd, int(ss), dst)
    return out


def show(name, cov):
    print("== %s ==" % name)
    print("    " + "".join(str(x % 10) for x in range(W)))
    for y in range(H):
        row = ""
        for x in range(W):
            v = cov[y * W + x]
            row += "." if v == 0 else ("#" if v > 200 else ("+" if v > 60 else ":"))
        print("%3d %s" % (y, row))


def main():
    a = py_fill(POLYS, W, H, RULE, SS)
    b = rust_multi(POLYS, W, H, RULE, SS)
    print("子路径点数:", [len(p) for p in POLYS])
    print("纯 Py 非零 %d  Rust 非零 %d  差异 %d" % (
        sum(1 for v in a if v), sum(1 for v in b if v),
        sum(1 for i in range(W * H) if a[i] != b[i])))
    print()
    show("纯 Python", a)
    print()
    show("Rust", b)


if __name__ == "__main__":
    main()

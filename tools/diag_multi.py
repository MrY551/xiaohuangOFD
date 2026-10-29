# -*- coding: utf-8 -*-
"""找出多子路径在真实页面上 Rust 与纯 Python 的第一个不一致调用。

做法：包一层 fill_coverage，每次同时用两条路算一遍，比到第一处不同就
把该次的多边形 dump 出来（能直接拿来写回归用例）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofdlite import OFDPackage, _fast  # noqa: E402
from ofdlite import raster as R  # noqa: E402
from ofdlite import render_soft as RS  # noqa: E402

WPS = "/mnt/c/Users/19240/WPSDrive/245473181/WPS云盘/市场运营部 2026年第三季度工作总结.ofd"
if not os.path.exists(WPS):
    WPS = "C:/Users/19240/WPSDrive/245473181/WPS云盘/市场运营部 2026年第三季度工作总结.ofd"

found = []


def rust_multi(polys, w, h, rule, ss):
    """直接调 Rust 的 multi 入口，绕开 _fast 里的回落判断。"""
    import ctypes
    # **必须清缓存**：_try_load() 会把 CDLL 存在 _fast._lib 里，
    # 重新编译 .so 之后不清缓存，测的还是旧二进制（这次踩过）。
    _fast._lib = None
    _fast.AVAILABLE = False
    lib = _fast._try_load()
    if lib is None:
        return None
    counts, coords = [], []
    for poly in polys:
        if len(poly) < 3:
            continue
        counts.append(len(poly))
        for (x, y) in poly:
            coords.append(float(x))
            coords.append(float(y))
    if not counts:
        return None
    out = bytearray(w * h)
    dst = (ctypes.c_ubyte * (w * h)).from_buffer(out)
    carr = (ctypes.c_int * len(counts))(*counts)
    farr = (ctypes.c_double * len(coords))(*coords)
    evenodd = 1 if str(rule).replace("-", "").replace("_", "").lower() == "evenodd" else 0
    lib.ofd_fill_coverage_multi(carr, len(counts), farr, w, h, evenodd, int(ss), dst)
    return out


orig = RS.fill_coverage


def probe(polys, w, h, rule="nonzero", ss=4):
    got = orig(polys, w, h, rule, ss)          # 现有路径（多子路径会回落纯 Py）
    if len([p for p in polys if len(p) >= 3]) > 1:
        alt = rust_multi(polys, w, h, rule, ss)
        if alt is not None and bytes(alt) != bytes(got):
            if not found:
                found.append((polys, w, h, rule, ss, got, alt))
    return got


def main():
    RS.fill_coverage = probe
    pkg = OFDPackage.open(WPS)
    try:
        for i, pg in enumerate(pkg.all_pages()):
            RS.render_page(pg, 96)
            if found:
                print("第 %d 页首次出现不一致" % (i + 1))
                break
    finally:
        RS.fill_coverage = orig

    if not found:
        print("没有发现不一致")
        return

    polys, w, h, rule, ss, a, b = found[0]
    print("参数: w=%d h=%d rule=%s ss=%s 子路径数=%d" % (w, h, rule, ss, len(polys)))
    for i, p in enumerate(polys):
        print("  子路径 %d: %d 点  %s" % (i, len(p), [(round(q[0], 2), round(q[1], 2)) for q in p[:6]]))
    diff = [i for i in range(w * h) if a[i] != b[i]]
    print("差异像素 %d / %d" % (len(diff), w * h))
    for i in diff[:8]:
        print("  (%d,%d) 纯Py=%d Rust=%d" % (i % w, i // w, a[i], b[i]))

    # 存成回归用例
    out = os.path.join(ROOT, "out", "multi_probe.py")
    with open(out, "w", encoding="utf-8") as f:
        f.write("# -*- coding: utf-8 -*-\n")
        f.write("POLYS = %r\n" % ([[(round(x, 4), round(y, 4)) for x, y in p] for p in polys],))
        f.write("W, H, RULE, SS = %d, %d, %r, %d\n" % (w, h, rule, ss))
    print("已存 %s" % out)


if __name__ == "__main__":
    main()

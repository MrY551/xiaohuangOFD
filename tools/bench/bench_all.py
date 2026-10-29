# -*- coding: utf-8 -*-
"""三个实现用同一份几何、同一个算法，只比语言。

几何：A4 @150dpi 的圆（720 段），模拟真实页面里最常见的抗锯齿填充。
算法与 ofdlite/raster.py 的 fill_coverage 完全一致（4 条子扫描线 + 非零环绕）。
"""
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofdlite.raster import fill_coverage  # noqa: E402

W, H, SS, NPTS = 1240, 827, 4, 720


def make_circle():
    cx, cy = W / 2.0, H / 2.0
    r = min(W, H) * 0.45
    pts = []
    for i in range(NPTS):
        a = 2.0 * math.pi * i / NPTS
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def bench_python(pts, repeat):
    polys = [pts]
    fill_coverage(polys, W, H, "nonzero", SS)  # 预热
    t0 = time.perf_counter()
    for _ in range(repeat):
        cov = fill_coverage(polys, W, H, "nonzero", SS)
    t1 = time.perf_counter()
    nz = sum(1 for v in cov if v)
    return (t1 - t0) * 1000.0 / repeat, nz


def run_bin(cmd):
    try:
        out = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                             timeout=600)
        line = (out.stdout or "").strip().splitlines()
        return line[-1] if line else (out.stderr or "").strip()[:80]
    except Exception as exc:
        return "失败: %s" % exc


def main():
    repeat_py = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    repeat_native = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    pts = make_circle()
    print("几何: %d 段圆的 A4 填充，画布 %dx%d，ss=%d" % (NPTS, W, H, SS))
    print()

    ms, nz = bench_python(pts, repeat_py)
    print("Python %d 次  平均 %.3f ms/次  非零 %d / %d" % (repeat_py, ms, nz, W * H))
    py_ms = ms

    # C
    c_src = os.path.join(HERE, "bench_c.c")
    c_bin = os.path.join(HERE, "bench_c")
    if not os.path.exists(c_bin) and os.path.exists(c_src):
        r = subprocess.run(["gcc", "-O2", "-o", c_bin, c_src, "-lm"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("C 编译失败:", (r.stderr or "")[:200])
    if os.path.exists(c_bin):
        print(run_bin([c_bin, str(repeat_native)]))

    # Rust
    rs_src = os.path.join(HERE, "bench_rs.rs")
    rs_bin = os.path.join(HERE, "bench_rs")
    if not os.path.exists(rs_bin) and os.path.exists(rs_src):
        r = subprocess.run(["rustc", "-O", "-o", rs_bin, rs_src],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("Rust 编译失败:", (r.stderr or "")[:300])
    if os.path.exists(rs_bin):
        print(run_bin([rs_bin, str(repeat_native)]))

    print()
    print("说明：三者算法完全相同（同样的 4 条子扫描线、同样的 span 累加、")
    print("      同样的 0~255 归一化），差异只来自语言本身。")


if __name__ == "__main__":
    main()

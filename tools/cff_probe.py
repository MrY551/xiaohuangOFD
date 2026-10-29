# -*- coding: utf-8 -*-
"""验证 CFF 轮廓能否正确渲染成文字（形状对不对，不只是点数够）。"""
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofdlite import ttf
from ofdlite.render_soft import Canvas

P = "/usr/share/fonts/opentype/urw-base35/NimbusMonoPS-Regular.otf"
f = ttf.load(P)
print("加载:", os.path.basename(P), "轮廓类型:", "CFF" if f._cff else "glyf")
print("upem=%d glyphs=%d cmap=%d" % (f.units_per_em, f.num_glyphs, len(f.cmap)))

cv = Canvas(900, 320)
k = 60.0 / f.units_per_em
x0, y0 = 40.0, 220.0
cx = x0
for ch in "ABC 123":
    gid = f.glyph_index(ch)
    if gid:
        polys = []
        for c in f.glyph_outlines(gid):
            pts = [(cx + px * k, y0 - py * k) for px, py in c]
            if len(pts) >= 3:
                polys.append(pts)
        if polys:
            cv.fill_polygons(polys, (20, 20, 20, 255), "nonzero", 4)
    cx += f.advance(gid) * k

out = os.path.join(ROOT, "out", "cff_text.png")
cv.to_png(out)
nz = sum(1 for i in range(0, len(cv.buf), 4) if cv.buf[i] < 128)
print("900x320 深色像素 %d -> %s" % (nz, out))

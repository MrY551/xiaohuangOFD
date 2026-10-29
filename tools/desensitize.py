# -*- coding: utf-8 -*-
"""截图脱敏：把真实票据里的敏感字段涂黑（不可逆）。

为什么用涂黑而不是模糊：模糊/马赛克可以被统计方法部分还原，
涂成纯色块才是真删。这里默认用纯黑填充。

用法：
    # 按内置规则处理（针对数电发票版式）
    python tools/desensitize.py 输入.png -o 输出.png --preset invoice

    # 手动指定区域（归一化坐标 0..1，左上角为原点）
    python tools/desensitize.py 输入.png -o 输出.png \
        --box 0.55,0.10,0.95,0.16 --box 0.10,0.28,0.55,0.36

    # 先看有哪些候选区域（不写文件）
    python tools/desensitize.py 输入.png --preset invoice --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("需要 Pillow: pip install pillow")
    sys.exit(2)


# 数电发票的固定版式（210×140mm 横向）。区域用归一化坐标，
# 换一张同版式的票也能直接套用。
INVOICE_BOXES = [
    # (x0, y0, x1, y1, 说明)
    (0.000, 0.030, 0.180, 0.230, "二维码（含票面信息）"),
    (0.735, 0.055, 0.995, 0.090, "发票号码"),
    (0.735, 0.115, 0.995, 0.150, "开票日期"),
    (0.075, 0.300, 0.420, 0.345, "购买方名称"),
    (0.075, 0.375, 0.420, 0.420, "购买方税号/识别号"),
    (0.480, 0.300, 0.900, 0.345, "销售方名称"),
    (0.660, 0.375, 0.995, 0.420, "销售方税号"),
    (0.075, 0.560, 0.760, 0.640, "项目明细（含服务名称）"),
    (0.640, 0.760, 0.995, 0.800, "合计金额"),
    (0.100, 0.820, 0.620, 0.870, "价税合计大写"),
    (0.660, 0.820, 0.995, 0.870, "价税合计小写"),
    (0.200, 0.910, 0.760, 0.955, "备注（账期、客户号码）"),
    (0.160, 0.960, 0.400, 0.995, "开票人"),
]


def parse_box(s: str):
    parts = [p.strip() for p in s.replace(";", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError("--box 需要 4 个归一化数值: x0,y0,x1,y1")
    x0, y0, x1, y1 = (float(p) for p in parts)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), "手动指定")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--preset", default="", choices=["", "invoice"])
    ap.add_argument("--box", action="append", default=[], help="归一化坐标 x0,y0,x1,y1，可多次")
    ap.add_argument("--color", default="#000000", help="遮盖色，默认纯黑（不可逆）")
    ap.add_argument("--pad", type=float, default=0.004, help="区域外扩比例，避免露边")
    ap.add_argument("--dry-run", action="store_true", help="只列区域，不写文件")
    args = ap.parse_args(argv)

    boxes = []
    if args.preset == "invoice":
        boxes += [b[:4] + (b[4],) for b in INVOICE_BOXES]
    for b in args.box:
        boxes.append(parse_box(b))
    if not boxes:
        print("没有指定区域：用 --preset invoice 或 --box")
        return 2

    img = Image.open(args.src).convert("RGB")
    W, H = img.size
    print("图片: %s  %d×%d" % (args.src, W, H))

    def px(b):
        x0, y0, x1, y1 = b[:4]
        pad = args.pad
        return (max(0, int((x0 - pad) * W)), max(0, int((y0 - pad) * H)),
                min(W, int((x1 + pad) * W)), min(H, int((y1 + pad) * H)))

    for b in boxes:
        r = px(b)
        print("  遮盖 %-28s px %s" % (b[4] if len(b) > 4 else "", r))

    if args.dry_run:
        print("（dry-run，未写文件）")
        return 0

    d = ImageDraw.Draw(img)
    for b in boxes:
        d.rectangle(px(b), fill=args.color)

    out = args.out or os.path.splitext(args.src)[0] + "-脱敏.png"
    img.save(out)
    print("已写出 %s（%d 字节）" % (out, os.path.getsize(out)))
    print("说明：涂黑不可逆，但请人工复查一遍原图，确认没有漏掉的字段")
    return 0


if __name__ == "__main__":
    sys.exit(main())

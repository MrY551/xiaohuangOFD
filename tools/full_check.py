# -*- coding: utf-8 -*-
"""整体验收：把所有测试文件、所有图元类型、所有填充类型都过一遍。

目的不是「跑通」，而是**找出还缺什么**——所以它统计的是
「哪些能力被真正触达过」「哪些还在告警」「哪些类型从没被覆盖」。
"""
from __future__ import annotations

import glob
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofdlite import OFDPackage
from ofdlite.scene import build_page_scene
from ofdlite.render_soft import render_page

REAL = "C:/Users/19240/OneDrive/文档/数电发票25357000000165684636.ofd"

# 期望被覆盖的能力（每项给出「怎么算触达」）
CAPS = {
    "rect 填充":        lambda ops: any(o.get("t") == "rect" and o.get("fill") for o in ops),
    "path 填充":        lambda ops: any(o.get("t") == "path" and o.get("fill") for o in ops),
    "path 描边":        lambda ops: any(o.get("t") == "path" and o.get("stroke") for o in ops),
    "虚线":             lambda ops: any(o.get("dash") for o in ops),
    "裁剪":             lambda ops: any(o.get("clip") for o in ops),
    "渐变-轴向":        lambda ops: any(isinstance(o.get("fill"), dict)
                                        and o["fill"].get("kind") == "axial" for o in ops),
    "渐变-径向":        lambda ops: any(isinstance(o.get("fill"), dict)
                                        and o["fill"].get("kind") == "radial" for o in ops),
    "渐变-高洛德":      lambda ops: any(isinstance(o.get("fill"), dict)
                                        and o["fill"].get("kind") == "gouraud" for o in ops),
    "渐变-网格":        lambda ops: any(isinstance(o.get("fill"), dict)
                                        and o["fill"].get("kind") == "lattice" for o in ops),
    "底纹":             lambda ops: any(isinstance(o.get("fill"), dict)
                                        and o["fill"].get("kind") == "pattern" for o in ops),
    "图片":             lambda ops: any(o.get("t") == "image" for o in ops),
    "图片边框":         lambda ops: any(o.get("t") == "image" and o.get("border") for o in ops),
    "文字":             lambda ops: any(o.get("t") == "text" and o.get("text") for o in ops),
    "文字粗体":         lambda ops: any(o.get("weight", 400) > 400 or o.get("bold")
                                        for o in ops if o.get("t") == "text"),
    "文字斜体":         lambda ops: any(o.get("italic") for o in ops if o.get("t") == "text"),
    "页面旋转":         lambda ops: None,   # 由 scene 的 rot 判定，单独查
    "evenodd 规则":     lambda ops: any((o.get("rule") or "").lower().startswith("even")
                                        for o in ops),
}


def collect_ops(pkg):
    ops = []
    rots = []
    for p in pkg.all_pages():
        try:
            sc = build_page_scene(p).to_dict()
        except Exception:
            continue
        ops.extend(sc.get("ops") or [])
        rots.append(int(sc.get("rot") or 0))
    return ops, rots


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "out", "*.ofd")))
    if os.path.exists(REAL):
        files.append(REAL)

    all_ops = []
    hits = {k: [] for k in CAPS}
    warns = {}
    fails = []
    rows = []
    total_t = 0.0
    for f in files:
        base = os.path.basename(f)
        try:
            t0 = time.time()
            pkg = OFDPackage.open(f)
            ops, rots = collect_ops(pkg)
            all_ops.extend(ops)
            if any(rots):
                hits["页面旋转"].append(base)
            for k, fn in CAPS.items():
                if k in ("页面旋转",):
                    continue
                try:
                    if fn(ops):
                        hits[k].append(base)
                except Exception:
                    pass
            for p in pkg.all_pages():
                cv, w = render_page(p, 96)
                if w:
                    warns.setdefault(base, []).extend(w)
                # 空白检测：有能力被「触达」但一个像素都没画出来的话，
                # 说明实现是坏的 —— 光统计 op 存在会漏掉这种。
                # 非白判定必须看三个通道：只看 R 的话纯红 (255,0,0) 会被当成白
                # （R=255），这正是 ofs_test 明明画了红块却被报"全白"的原因。
                nz = sum(1 for k in range(0, len(cv.buf), 4)
                         if not (cv.buf[k] > 250 and cv.buf[k + 1] > 250
                                 and cv.buf[k + 2] > 250))
                if nz == 0 and ops:
                    fails.append((base, "渲染结果全白（%d 个图元）" % len(ops)))
            total_t += time.time() - t0
            rows.append((base, len(ops), "ok"))
        except Exception as exc:
            fails.append((base, str(exc)[:80]))
            rows.append((base, 0, "FAIL"))

    print("=" * 66)
    print("测试文件 %d 份，图元总数 %d，总耗时 %.1f 秒" % (
        len(files), len(all_ops), total_t))
    print("=" * 66)
    for name, n, st in rows:
        mark = "ok " if st == "ok" else "FAIL"
        print("  %s  %-28s %4d 个图元" % (mark, name, n))

    print()
    print("=" * 66)
    print("能力覆盖（哪份文件真正触达了它）")
    print("=" * 66)
    missing = []
    for k in CAPS:
        got = hits.get(k) or []
        if got:
            print("  ✅ %-14s  %s" % (k, ", ".join(got[:3]) + ("..." if len(got) > 3 else "")))
        else:
            print("  ❌ %-14s  **没有任何测试文件触达**" % k)
            missing.append(k)

    print()
    print("=" * 66)
    print("渲染告警")
    print("=" * 66)
    if not warns:
        print("  无")
    else:
        seen = set()
        for f, ws in warns.items():
            for w in ws:
                if w in seen:
                    continue
                seen.add(w)
                print("  %-24s %s" % (f, w))

    print()
    if fails:
        print("=" * 66)
        print("打不开 / 渲染失败")
        print("=" * 66)
        for f, e in fails:
            print("  %-28s %s" % (f, e))

    print()
    print("结论：%s" % (
        "全部达标" if not missing and not warns and not fails
        else "有 %d 项未被覆盖、%d 条告警、%d 份失败" % (
            len(missing), len(warns), len(fails))))
    return 0 if (not missing and not warns and not fails) else 1


if __name__ == "__main__":
    sys.exit(main())

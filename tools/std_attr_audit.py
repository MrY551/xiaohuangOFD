# -*- coding: utf-8 -*-
"""属性级标准覆盖审计：拿附录 A 的 XSD 当权威清单，逐属性查代码里有没有处理。

为什么不用 docs/33190-2016-*.txt 正则抽属性：PDF 抽出来的表是逐列断行的，
属性名和说明混在一起，正则抽不准。附录 A 的 XSD 是标准自带的机器可读形态，
属性名/类型/默认值都在，正好当**核对基准**（标准正文与 XSD 打架的地方，
本脚本把两边的说法都打出来，不替谁下结论）。

用法：
    python tools/std_attr_audit.py                # 打到终端
    python tools/std_attr_audit.py -m docs/标准属性覆盖.md

判定口径（必须说清楚，否则这张表会被当成功劳簿）：
    引用 = 代码里出现了这个属性名的字面量（属性表 / .get("X") / JS 的 el.getAttribute 等）
    这只能证明"读过"，不能证明"用对了"。真正算数的仍是 test 与像素比对。
"""
from __future__ import annotations

import argparse
import collections
import io
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
XSD_DIR = os.path.join(ROOT, "docs", "xsd")
CODE_DIRS = ["ofdlite", "app", os.path.join("web", "static")]
EXTS = (".py", ".js")

XS = "{http://www.w3.org/2001/XMLSchema}"


def parse_xsd():
    """返回 [(attr, [owner...], default)]，owner 是具名 complexType 或最近的元素名。"""
    found = collections.OrderedDict()
    for fn in sorted(os.listdir(XSD_DIR)):
        if not fn.endswith(".xsd"):
            continue
        root = ET.parse(os.path.join(XSD_DIR, fn)).getroot()

        def walk(node, path, named):
            tag = node.tag.split("}")[-1]
            if tag == "complexType":
                nm = node.get("name")
                named = nm if nm else named
                if nm:
                    path = path + [nm]
            elif tag == "element" and node.get("name") and "name" not in ():
                path = path + [node.get("name")]
            elif tag == "attribute":
                attr = node.get("name")
                owner = named or (path[-1] if path else "?")
                if attr not in found:
                    found[attr] = {"owners": [], "default": None, "src": fn}
                if owner not in found[attr]["owners"]:
                    found[attr]["owners"].append(owner)
                if node.get("default") is not None:
                    found[attr]["default"] = node.get("default")
            for c in node:
                walk(c, path, named)

        walk(root, [], None)
    return found


def load_code():
    files = []
    for base in CODE_DIRS:
        d = os.path.join(ROOT, base)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(EXTS):
                p = os.path.join(d, fn)
                try:
                    files.append((os.path.relpath(p, ROOT),
                                  io.open(p, encoding="utf-8", errors="replace").readlines()))
                except OSError:
                    pass
    return files


def find_usage(attr, files):
    """字面量匹配：属性名两侧不能被字母数字包住（避免 Type 命中 TypeName）。"""
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(attr) + r"(?![A-Za-z0-9_])")
    hits = []
    for name, lines in files:
        for i, ln in enumerate(lines, 1):
            if pat.search(ln):
                hits.append((name, i, ln.strip()[:110]))
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--md", default="", help="同时写一份 markdown")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印命中行")
    args = ap.parse_args(argv)

    attrs = parse_xsd()
    files = load_code()
    rows = []
    for attr, info in attrs.items():
        hits = find_usage(attr, files)
        rows.append((attr, info, hits))

    miss = [r for r in rows if not r[2]]
    print("XSD 属性总数: %d  有引用: %d  未引用: %d"
          % (len(rows), len(rows) - len(miss), len(miss)))
    print("")
    print("=== 未引用 ===")
    for attr, info, _ in miss:
        print("  %-26s owners=%-30s default=%s"
              % (attr, ",".join(info["owners"])[:30], info["default"]))
    print("")
    print("=== 有引用（只列前 3 处） ===")
    for attr, info, hits in rows:
        if not hits:
            continue
        print("  %-24s %s" % (attr, "; ".join("%s:%d" % (h[0], h[1]) for h in hits[:3])))
        if args.verbose:
            for h in hits[:3]:
                print("        %s" % h[2])

    if args.md:
        out = ["# XSD 属性覆盖（GB/T 33190-2016 附录 A）", ""]
        out.append("由 `tools/std_attr_audit.py` 生成。属性清单取自 `docs/xsd/*.xsd`，"
                   "「有引用」只表示代码里出现过该属性名，不等于用对了。")
        out.append("")
        out.append("XSD 属性 %d 个：有引用 **%d**，未引用 **%d**。"
                   % (len(rows), len(rows) - len(miss), len(miss)))
        out.append("")
        out.append("## 未引用（代码里完全没有这个属性名）")
        out.append("")
        out.append("| 属性 | 所属类型 | XSD 默认值 |")
        out.append("|---|---|---|")
        for attr, info, _ in miss:
            out.append("| `%s` | %s | %s |"
                       % (attr, ", ".join(info["owners"]), info["default"] or ""))
        out.append("")
        io.open(args.md, "w", encoding="utf-8").write("\n".join(out))
        print("\n已写出 %s" % os.path.relpath(args.md, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())

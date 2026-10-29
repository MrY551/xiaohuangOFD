# -*- coding: utf-8 -*-
"""同源度取证：判断一份副本是不是本项目换皮而来。

三个指标，都能复跑，都能写进函件：

  1. 专有命名重合率 —— 长度 ≥8 的自定义标识符里，有多少逐字出现在对方代码中。
     这个指标对付"改注释、改格式、重排文件"的换皮特别有效：注释能改，函数名和
     类名改起来成本高，改了就调用不通。

  2. 中文长句重合率 —— ≥6 个连续汉字的长串里，有多少逐字相同。
     中文项目的强指标：注释、提示语、文档字符串、界面文案都在里面，
     原创实现不可能逐字撞这么多。

  3. 有效行重合率 —— 归一化后的代码行（≥12 字符）哈希交集。

判定口径（参考开源社区取证实践）：

    ┌──────────────┬──────────────────┬──────────────────┐
    │ 专有命名     │ 中文长句         │ 结论             │
    ├──────────────┼──────────────────┼──────────────────┤
    │ > 50%        │ > 40%            │ 高度同源（换皮） │
    │ 20% ~ 50%    │ 10% ~ 40%        │ 需人工复核       │
    │ < 20%        │ < 10%            │ 不构成同源       │
    └──────────────┴──────────────────┴──────────────────┘

用法：
    python3 tools/forensics.py --suspect /path/to/副本
    python3 tools/forensics.py --suspect /path/to/副本 --json out.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SOURCE_EXT = (".py", ".js", ".mjs", ".ts", ".vue", ".sh")
TEXT_EXT = SOURCE_EXT + (".md", ".txt", ".html", ".css", ".json")

SKIP_DIRS = {"__pycache__", ".git", ".github", "docs", "out", "dist", "build",
             "node_modules", "tests", "test"}

# Python / JS 的内置名与通用词，不参与"专有"统计
COMMON = {
    "staticmethod", "classmethod", "property", "abstractmethod",
    "isinstance", "getattr", "setattr", "hasattr", "Exception", "ValueError",
    "TypeError", "KeyError", "IndexError", "RuntimeError", "function",
    "constructor", "prototype", "addEventListener", "querySelector",
    "getElementById", "appendChild", "createElement", "textContent",
    "toString", "indexOf", "forEach", "splice", "filter", "reduce",
    "__init__", "__name__", "__main__", "__version__", "__all__",
    "encoding", "errors", "replace", "strip", "split", "join", "format",
}

IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{7,})\b")
CJK_RE = re.compile(r"[\u4e00-\u9fff]{6,}")


def walk(root: str, exts):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            if f.endswith(exts):
                yield os.path.join(dirpath, f)


def read(path: str) -> str:
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def proprietary_names(root: str) -> set:
    """长度 ≥8、非通用词、且在注释与字符串之外也出现的标识符。

    简化处理：只看源码文件的标识符词表，剔除 COMMON 与全大写常量。
    """
    out = set()
    for path in walk(root, SOURCE_EXT):
        text = read(path)
        for m in IDENT_RE.finditer(text):
            w = m.group(1)
            if w in COMMON:
                continue
            # 纯小写单音节 / 纯大写缩写不算专有
            if w.isupper() and len(w) <= 10:
                continue
            out.add(w)
    return out


def chinese_phrases(root: str) -> set:
    """≥6 个连续汉字的长串（注释、提示语、文档字符串、界面文案）。"""
    out = set()
    for path in walk(root, TEXT_EXT):
        text = read(path)
        for m in CJK_RE.finditer(text):
            out.add(m.group(0))
    return out


def line_hashes(root: str) -> set:
    out = set()
    for path in walk(root, SOURCE_EXT):
        for raw in read(path).splitlines():
            s = raw.strip()
            if not s or s.startswith(("#", "//", "/*", "*", "<!--", '"""', "'''")):
                continue
            s = re.sub(r"\s+", " ", s)
            if len(s) < 12:
                continue
            out.add(hashlib.md5(s.encode("utf-8")).hexdigest())
    return out


def pct(a: int, b: int) -> float:
    return 100.0 * a / max(1, b)


def judge(name_pct: float, cjk_pct: float) -> str:
    if name_pct > 50 or (name_pct > 30 and cjk_pct > 40):
        return "高度同源：判定为换皮二改"
    if name_pct > 20 or cjk_pct > 10:
        return "存在可疑重合：需人工复核具体文件"
    return "不构成同源"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mine", default=ROOT, help="本项目根目录")
    ap.add_argument("--suspect", required=True, help="被怀疑的副本目录")
    ap.add_argument("--json", default="", help="把结果写成 JSON")
    ap.add_argument("--sample", type=int, default=8, help="每类最多列多少条重合项")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.suspect):
        print("找不到副本目录:", args.suspect)
        return 2

    print("=" * 68)
    print("同源度取证")
    print("  本项目:", os.path.abspath(args.mine))
    print("  副本  :", os.path.abspath(args.suspect))
    print("=" * 68)

    mine_n = proprietary_names(args.mine)
    sus_n = proprietary_names(args.suspect)
    n_hit = mine_n & sus_n
    n_pct = pct(len(n_hit), len(mine_n))
    print()
    print("[1] 专有命名（≥8 字符，已排除通用词）")
    print("    本项目 %d 个 / 副本 %d 个 / 逐字重合 %d 个 = %.1f%%"
          % (len(mine_n), len(sus_n), len(n_hit), n_pct))
    for x in sorted(n_hit)[:args.sample]:
        print("      ·", x)

    mine_c = chinese_phrases(args.mine)
    sus_c = chinese_phrases(args.suspect)
    c_hit = mine_c & sus_c
    c_pct = pct(len(c_hit), len(mine_c))
    print()
    print("[2] 中文长句（≥6 连续汉字）")
    print("    本项目 %d 条 / 副本 %d 条 / 逐字相同 %d 条 = %.1f%%"
          % (len(mine_c), len(sus_c), len(c_hit), c_pct))
    for x in sorted(c_hit, key=len, reverse=True)[:args.sample]:
        print("      ·", x[:60])

    mine_l = line_hashes(args.mine)
    sus_l = line_hashes(args.suspect)
    l_hit = mine_l & sus_l
    l_pct = pct(len(l_hit), len(mine_l))
    print()
    print("[3] 有效行（≥12 字符，归一化后哈希）")
    print("    本项目 %d 行 / 副本 %d 行 / 完全相同 %d 行 = %.1f%%"
          % (len(mine_l), len(sus_l), len(l_hit), l_pct))

    verdict = judge(n_pct, c_pct)
    print()
    print("=" * 68)
    print("判定：%s" % verdict)
    print("=" * 68)

    if args.json:
        data = {
            "mine": os.path.abspath(args.mine),
            "suspect": os.path.abspath(args.suspect),
            "proprietary_names": {"mine": len(mine_n), "suspect": len(sus_n),
                                  "overlap": len(n_hit), "percent": round(n_pct, 2),
                                  "sample": sorted(n_hit)[:50]},
            "chinese_phrases": {"mine": len(mine_c), "suspect": len(sus_c),
                                "overlap": len(c_hit), "percent": round(c_pct, 2),
                                "sample": sorted(c_hit, key=len, reverse=True)[:50]},
            "line_hashes": {"mine": len(mine_l), "suspect": len(sus_l),
                            "overlap": len(l_hit), "percent": round(l_pct, 2)},
            "verdict": verdict,
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("结果已写入:", args.json)

    print()
    print("复跑命令：")
    print("  python3 tools/forensics.py --suspect %s" % args.suspect)
    return 0


if __name__ == "__main__":
    sys.exit(main())

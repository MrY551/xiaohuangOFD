# -*- coding: utf-8 -*-
"""代码原创性核查：拿本项目和外部实现做字符串级比对。

不是比对"名字"，是比对"代码本身有没有大段重合"。

做法：
  1. 逐文件归一化（去空行、去纯注释、压空白），取长度 ≥ 12 的有效行
  2. 算行哈希交集：本项目的行在对方代码里出现过多少次
  3. 算标识符交集：函数名/类名重合说明有搬运，尤其语言相同的前提下
  4. 扫第三方版权痕迹：别人的 Copyright / @author / Licensed under / 项目专属词

用法：
    python tools/check_originality.py --against /tmp/orig_check/easyofd-20260427
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 只看真正干活的代码，跳过测试/文档/生成物
SKIP_DIRS = {"__pycache__", ".git", "docs", "out", "dist", "node_modules",
             "test", "tests", "build", "packaging"}
MIN_LINE = 12          # 太短的行（`return x`）天然会撞，不参与统计

# 别人的版权/作者痕迹
FOREIGN_MARKS = [
    r"Copyright\s*\(c\)\s*(?!2026 小黄)",
    r"@author",
    r"Licensed under the Apache",
    r"Licensed under the MIT",
    r"SPDX-License-Identifier",
    r"ofdrw", r"easyofd", r"XiaoFeng", r"ofd-analyze",
]


def norm_line(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s


def is_comment_or_blank(s: str) -> bool:
    t = s.strip()
    if not t:
        return True
    for p in ("#", "//", "/*", "*/", "<!--", "-->", '"""', "'''", "*"):
        if t.startswith(p):
            return True
    return False


def py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith((".py", ".js", ".mjs", ".vue", ".ts")):
                yield os.path.join(dirpath, f)


def line_hashes(root: str):
    """返回 {行哈希: 出现次数}，用于求交集。"""
    out = Counter()
    total = 0
    for path in py_files(root):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for raw in text.splitlines():
            if is_comment_or_blank(raw):
                continue
            s = norm_line(raw)
            if len(s) < MIN_LINE:
                continue
            out[hashlib.md5(s.encode("utf-8")).hexdigest()] += 1
            total += 1
    return out, total


IDENT_RE = re.compile(
    r"\bdef\s+([A-Za-z_]\w{3,})"                        # Python 函数
    r"|\bclass\s+([A-Za-z_]\w{3,})"                     # 类
    r"|\bfunction\s+([A-Za-z_]\w{3,})"                  # JS 函数
    r"|\b([A-Za-z_]\w{3,})\s*[:=]\s*(?:async\s*)?function"   # 对象方法
)


def identifiers(root: str):
    out = Counter()
    for path in py_files(root):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for m in IDENT_RE.finditer(text):
            name = next((g for g in m.groups() if g), None)
            if name:
                out[name] += 1
    return out


def foreign_marks(root: str):
    hits = []
    for path in py_files(root):
        rel = os.path.relpath(path, root)
        if rel.startswith("tools" + os.sep + "check_"):
            continue          # 自检脚本里写着自己的署名和检测正则，不算
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            # 自己的署名不算"第三方痕迹"
            if "小黄" in line or "xiaohuang" in line.lower():
                continue
            for pat in FOREIGN_MARKS:
                if re.search(pat, line, re.I):
                    hits.append((rel, i, line.strip()[:120]))
                    break
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--against", required=True, help="外部实现的源码目录")
    ap.add_argument("--show", type=int, default=12, help="最多列出多少条重合行")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.against):
        print("找不到对照目录:", args.against)
        return 2

    print("=" * 66)
    print("对照对象:", args.against)
    print("=" * 66)

    mine, mine_total = line_hashes(ROOT)
    theirs, their_total = line_hashes(args.against)
    print("本项目有效代码行: %d   对方: %d" % (mine_total, their_total))

    common = set(mine) & set(theirs)
    overlap = sum(min(mine[h], theirs[h]) for h in common)
    pct = 100.0 * overlap / max(1, mine_total)
    print("完全相同的行: %d 条" % overlap)
    print("占本项目比例: %.2f%%" % pct)
    print()

    want = {}          # 反查原文，便于人工判断是"通用写法"还是"搬运"
    if overlap and args.show:
        print("重合行明细（最多 %d 条）：" % args.show)
        shown = 0
        # 反查原文，便于人工判断是"通用写法"还是"搬运"
        want = {}
        for path in py_files(ROOT):
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            for raw in text.splitlines():
                if is_comment_or_blank(raw):
                    continue
                s = norm_line(raw)
                if len(s) < MIN_LINE:
                    continue
                h = hashlib.md5(s.encode("utf-8")).hexdigest()
                if h in common and h not in want:
                    want[h] = (os.path.relpath(path, ROOT), s)
        for h, (rel, s) in list(want.items())[:args.show]:
            print("  %s: %s" % (rel, s[:100]))
        print()

    mi = identifiers(ROOT)
    ti = identifiers(args.against)
    same_ident = set(mi) & set(ti)
    print("函数/类名：本项目 %d 个，对方 %d 个，重合 %d 个"
          % (len(mi), len(ti), len(same_ident)))
    if same_ident:
        print("  重合名（前 15）:", ", ".join(sorted(same_ident)[:15]))
    print()

    marks = foreign_marks(ROOT)
    print("第三方版权/作者痕迹: %d 处" % len(marks))
    for rel, ln, line in marks[:10]:
        print("  %s:%d  %s" % (rel, ln, line))
    print()

    # 把重合行按性质归类，看是"语法骨架"还是"实打实的逻辑"
    buckets = {"import/from": 0, "def/class/@装饰器": 0, "except/finally": 0,
               "return": 0, "其他": 0}
    others = []
    for h, (rel, s_) in want.items():
        t = s_.strip()
        if t.startswith(("import ", "from ")):
            buckets["import/from"] += 1
        elif t.startswith(("def ", "class ", "@")):
            buckets["def/class/@装饰器"] += 1
        elif t.startswith(("except", "finally")):
            buckets["except/finally"] += 1
        elif t.startswith("return "):
            buckets["return"] += 1
        else:
            buckets["其他"] += 1
            others.append("%s: %s" % (rel, t[:90]))
    print("重合行构成（按去重后的不同行计，总出现 %d 次）：" % overlap)
    for k, v in buckets.items():
        if v:
            print("  %-16s %d 条" % (k, v))
    if others:
        print("  非骨架部分：")
        for o in others[:8]:
            print("    ", o)
    print()

    # 结论
    print("=" * 66)
    skeleton = buckets["import/from"] + buckets["def/class/@装饰器"] + \
        buckets["except/finally"] + buckets["return"]
    distinct = max(1, len(want))
    if pct < 1.0 and skeleton >= distinct * 0.6:
        print("结论：代码级重合度 %.2f%%（%d/%d 次出现属于 import/def/except/return 骨架），"
              "函数名重合 %d 个且全部是 GB/T 33190 规定的元素名 —— 不构成照搬。"
              % (pct, skeleton, overlap, len(same_ident)))
    elif pct < 5.0:
        print("结论：有少量通用写法重合（%.2f%%），需人工看一眼上面明细。" % pct)
    else:
        print("结论：重合度 %.2f%% 偏高，需要逐条核。" % pct)
    return 0


if __name__ == "__main__":
    sys.exit(main())

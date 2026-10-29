# -*- coding: utf-8 -*-
"""生成 SHA256SUMS.txt —— 发布时给出一份可核对的完整清单。

为什么需要它：BUILD_ID 只覆盖源码树的聚合指纹，粒度太粗；
SHA256SUMS 逐文件给出哈希，用户下载后可以精确核对"我手上这个文件有没有被动过"。

用法：
    python3 tools/gen_sha256sums.py                  # 写 SHA256SUMS.txt
    python3 tools/gen_sha256sums.py --include-deb    # 连 dist/*.deb 一起
    python3 tools/gen_sha256sums.py --check          # 校验现有文件是否与清单一致

校验方法（写进清单头部，给用户看）：
    Linux   : sha256sum -c SHA256SUMS.txt
    macOS   : shasum -a 256 -c SHA256SUMS.txt
    Windows : Get-FileHash 文件 -Algorithm SHA256
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "SHA256SUMS.txt")

# 纳入清单的目录与扩展名
INCLUDE_DIRS = ("ofdlite", "app", "web", "tools", "docs", "packaging", ".github")
INCLUDE_ROOT = ("README.md", "LICENSE", "THIRD-PARTY-NOTICES.md",
                "pyproject.toml", ".gitignore", ".gitattributes")
INCLUDE_EXT = (".py", ".js", ".mjs", ".css", ".html", ".md", ".sh", ".toml",
               ".yml", ".yaml", ".xsd", ".png", ".c", ".h")

SKIP_DIRS = {"__pycache__", ".git", "out", "dist", "build", "node_modules"}

HEADER = """# 小黄OFD —— SHA256 校验清单
# 项目: https://github.com/MrY551/xiaohuangOFD
# 生成: python3 tools/gen_sha256sums.py
#
# 校验方法：
#   Linux/macOS : sha256sum -c SHA256SUMS.txt
#   Windows     : Get-FileHash <文件> -Algorithm SHA256
#
# 说明：本清单不包含 SHA256SUMS.txt 自身（自指会变）；
#       BUILD_ID 见 `ofd-cli --version` 或 /usr/share/doc/xiaohuang-ofd/BUILD_ID
"""


TEXT_EXT = (".py", ".md", ".js", ".mjs", ".css", ".html", ".sh", ".toml",
            ".yml", ".yaml", ".xsd", ".txt", "LICENSE", ".gitignore", ".gitattributes")


def sha256(path: str) -> str:
    """文本文件按 LF 归一化后计算，二进制原样。

    为什么必须归一化：Windows 工作区是 CRLF、Linux/CI checkout 是 LF，
    不归一化的话同一份源码在两个平台上算出不同哈希，
    用户从 GitHub 下载后 `sha256sum -c` 会全部报错（CI 里已实测踩到）。
    """
    data = open(path, "rb").read()
    name = os.path.basename(path)
    if name.endswith(TEXT_EXT) or name in ("LICENSE", "COPYING",
                                          ".gitignore", ".gitattributes"):
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def collect(include_deb: bool):
    items = []
    for name in INCLUDE_ROOT:
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            items.append((name, p))
    for d in INCLUDE_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(x for x in dirnames if x not in SKIP_DIRS)
            for f in sorted(filenames):
                if not f.endswith(INCLUDE_EXT):
                    continue
                p = os.path.join(dirpath, f)
                rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
                items.append((rel, p))
    if include_deb:
        dist = os.path.join(ROOT, "dist")
        if os.path.isdir(dist):
            for f in sorted(os.listdir(dist)):
                if f.endswith(".deb"):
                    items.append(("dist/" + f, os.path.join(dist, f)))
    items.sort(key=lambda x: x[0])
    return items


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-deb", action="store_true")
    ap.add_argument("--check", action="store_true", help="只校验，不重写")
    args = ap.parse_args(argv)

    items = collect(args.include_deb)

    if args.check:
        bad = missing = 0
        for rel, path in items:
            if not os.path.exists(path):
                print("  缺文件:", rel)
                missing += 1
                continue
            want = None
            # 严格格式：64 位十六进制 + 两个空格 + 路径。
            # 不能用 endswith 匹配——清单头部注释里恰好写着生成脚本的路径，
            # 会把注释行当成数据行（踩过一次）。
            for line in open(OUT, encoding="utf-8"):
                m = re.match(r"^([0-9a-f]{64})  (.+)$", line.rstrip("\n"))
                if m and m.group(2) == rel:
                    want = m.group(1)
                    break
            got = sha256(path)
            if want and want != got:
                print("  已改动:", rel)
                print("    清单:", want[:16])
                print("    实际:", got[:16])
                bad += 1
        print("校验完成：改动 %d / 缺失 %d / 合计 %d" % (bad, missing, len(items)))
        return 1 if (bad or missing) else 0

    lines = [HEADER]
    for rel, path in items:
        lines.append("%s  %s\n" % (sha256(path), rel))
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(lines))
    print("写出 %s：%d 个文件" % (os.path.relpath(OUT, ROOT), len(items)))
    print("校验：sha256sum -c SHA256SUMS.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())

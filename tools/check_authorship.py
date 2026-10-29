# -*- coding: utf-8 -*-
"""署名自检：确认 LICENSE 里列的那些位置真的写着作者信息。

LICENSE 第「署名位置一览」一节列了五处，这个脚本逐处核对；
有人把署名删了或换掉，跑一下就能发现。

    python tools/check_authorship.py            # 查源码与文档
    python tools/check_authorship.py --deb dist/xiaohuang-ofd_1.0.0_all.deb

退出码：全在 0，缺任何一处 1。
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

NAME = "小黄"
PHONE = "15359927763"
QQ = "1924053793"

# (文件, 必须出现的片段, 说明)
SOURCES = [
    ("LICENSE", "Copyright (c) 2026 小黄 <15359927763> QQ 1924053793", "许可文件里的版权行"),
    ("LICENSE", "15359927763", "联系方式：手机"),
    ("LICENSE", "1924053793", "联系方式：QQ"),
    ("ofdlite/buildid.py", "15359927763", "CLI --version 的作者行（banner）"),
    ("app/mainwindow.py", "15359927763", "桌面端关于框"),
    ("web/static/index.html", "15359927763", "网页端页脚"),
    ("packaging/build_deb.sh", "1924053793@qq.com", "deb Maintainer"),
    ("README.md", "小黄", "文档署名"),
    ("THIRD-PARTY-NOTICES.md", "小黄OFD", "依赖声明里的项目名"),
]


def check_sources() -> int:
    bad = 0
    for rel, needle, desc in SOURCES:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            print("  MISS %-28s %s（文件不存在）" % (rel, desc))
            bad += 1
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        if needle in text:
            print("  ok   %-28s %s" % (rel, desc))
        else:
            print("  FAIL %-28s %s（找不到「%s」）" % (rel, desc, needle))
            bad += 1
    return bad


def _to_wsl(path: str) -> str:
    """Windows 路径 -> WSL 里的 /mnt/x/...，否则 WSL 的 cwd 不同会找不到文件。"""
    if os.name != "nt":
        return path
    ap = os.path.abspath(path).replace("\\", "/")
    if len(ap) > 1 and ap[1] == ":":
        return "/mnt/" + ap[0].lower() + ap[2:]
    return ap


def _run(cmd: str) -> str:
    """整条命令交给 shell 执行（要管道），Windows 下转进 WSL。"""
    import subprocess
    kw = dict(capture_output=True, encoding="utf-8", errors="replace")
    if os.name == "nt":
        return subprocess.run(["wsl", "-d", "shuseek", "-e", "bash", "-lc", cmd],
                              **kw).stdout or ""
    return subprocess.run(["bash", "-lc", cmd], **kw).stdout or ""


def check_deb(path: str) -> int:
    bad = 0
    if not os.path.isfile(path):
        print("  找不到 deb:", path)
        return 1
    path = _to_wsl(path)

    maint = _run("dpkg-deb -f '%s' Maintainer" % path).strip()
    if QQ in maint:
        print("  ok   deb Maintainer            %s" % maint)
    else:
        print("  FAIL deb Maintainer            没有 %s：%s" % (QQ, maint))
        bad += 1

    cop = _run("dpkg-deb --fsys-tarfile '%s' | tar -xO ./usr/share/doc/xiaohuang-ofd/copyright"
               % path)
    for needle, desc in ((PHONE, "手机"), (QQ, "QQ"), (NAME, "作者")):
        if needle in cop:
            print("  ok   deb copyright  %-12s %s" % (needle, desc))
        else:
            print("  FAIL deb copyright  %-12s %s（%d 字节）" % (needle, desc, len(cop)))
            bad += 1

    desk = _run("dpkg-deb --fsys-tarfile '%s' | tar -xO ./usr/share/applications/ofd-editor.desktop"
                % path)
    if "小黄OFD" in desk:
        print("  ok   deb desktop    Name=小黄OFD")
    else:
        print("  FAIL deb desktop    Name 里没有品牌名")
        bad += 1
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--deb", default="", help="顺带检查打包产物")
    args = ap.parse_args(argv)

    print("源码与文档：")
    bad = check_sources()
    if args.deb:
        print("\n安装包：")
        bad += check_deb(args.deb)

    print()
    if bad:
        print("署名缺失 %d 处" % bad)
        return 1
    print("署名齐全")
    return 0


if __name__ == "__main__":
    sys.exit(main())

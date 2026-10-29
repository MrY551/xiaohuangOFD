# -*- coding: utf-8 -*-
"""构建指纹：把当前源码树压成一个短哈希。

用途：官方发布时公开一次这个值，之后任何人改过源码，算出来的指纹就对不上。
它不能阻止别人删署名，但能让「这份是不是原版」变成可核对的事实——
分发一个署名被抹掉的副本时，对方拿不出与官方发布记录一致的指纹。

    python3 -m ofdlite.buildid          # 打印
    python3 -c "from ofdlite import build_id; print(build_id())"
"""
from __future__ import annotations

import hashlib
import os

# 参与指纹的文件：包内全部 .py，按路径排序保证结果稳定
_SKIP_DIRS = {"__pycache__", ".git", "build", "dist", "out"}


def _root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for f in sorted(filenames):
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def source_digest(root: str = "") -> str:
    """对包内所有 .py 的内容求聚合哈希（路径 + 内容都参与）。"""
    root = root or _root()
    h = hashlib.sha256()
    for path in _py_files(root):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            data = open(path, "rb").read()
        except OSError:
            data = b"<unreadable>"
        # 归一化换行：Windows checkout（CRLF）与 Linux（LF）必须算出同一指纹，
        # 否则同一份源码在两个平台上 BUILD_ID 不同，核对就失效了
        h.update(data.replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest()


def build_id(short: int = 12) -> str:
    """形如 1.0.0+g1a2b3c4d5e6f，官方发布记录里的就是这个串。"""
    from ofdlite import __version__
    return "%s+g%s" % (__version__, source_digest()[:short])


def _kernel_line() -> str:
    """渲染内核状态行。没装 Rust 内核时如实说明走纯 Python，不隐藏。"""
    try:
        from ofdlite import _fast
        ok, msg = _fast.status()
        return "渲染内核 %s\n" % msg
    except Exception:
        return ""


def banner() -> str:
    """给 --version / 关于框用的三行。"""
    from ofdlite import __version__
    return (
        "小黄OFD %s\n" % __version__
        + "BUILD_ID %s\n" % build_id()
        + _kernel_line()
        + "Copyright (c) 2026 小黄 <15359927763> QQ 1924053793\n"
        + "本软件许可禁止删除或替换作者署名后以其他名义发布（见 LICENSE）"
    )


if __name__ == "__main__":
    print(build_id())

# -*- coding: utf-8 -*-
"""字体发现与映射。

OFD 里写的是字体名（"宋体"、"SimSun"、"方正书宋_GBK"），而系统里装的是
另一套名字。这里做两件事：
  1. 扫描系统字体目录，建 family 名 → 文件 的索引（结果缓存到进程内）
  2. 把 OFD 里的常见字体名映射到本机可用字体，逐级降级
     （内嵌字体 → 精确同名 → 别名表 → 同族 → 中文默认 → 任何可用字体）

不做联网取字体。缺字时后端渲染成空框，并在 warnings 里报出来。
"""
from __future__ import annotations

import glob
import os
import re
from typing import Dict, List, Optional, Tuple

# Windows/麒麟上的字体文件名和 OFD 里写的字体名没有字面重合：
# OFD 写"楷体"，文件叫 simkai.ttf；写"微软雅黑"，文件叫 msyh.ttc。
# 只按名字查会全部落空，然后统一退化成宋体——票头、税号这些地方就全错了。
# 这张表把中文名直接映射到文件名前缀。
FILE_HINTS = {
    "楷体": ["simkai", "stkaiti", "kaiti"],
    "楷体_gb2312": ["simkai", "stkaiti"],
    "宋体": ["simsun", "songti", "stsong"],
    "新宋体": ["nsimsun"],
    "黑体": ["simhei", "stheiti", "heiti"],
    "仿宋": ["simfang", "fangsong", "stfangsong"],
    "仿宋_gb2312": ["simfang", "fangsong"],
    "微软雅黑": ["msyh", "yahei"],
    "等线": ["deng", "dengxian"],
    "隶书": ["lisu"],
    "幼圆": ["youyuan"],
    "华文楷体": ["stkaiti"],
    "华文宋体": ["stsong"],
    "华文黑体": ["stheiti"],
    "华文中宋": ["stzhongsong"],
    "方正书宋": ["fzss", "fzshusong"],
    "方正黑体": ["fzhei", "fzheiti"],
    "方正楷体": ["fzkai", "fzkaiti"],
    "方正仿宋": ["fzfs", "fzfangsong"],
}

# 常见别名：OFD 里的名字 → 本机候选（按优先级）
ALIASES = {
    "宋体": ["SimSun", "Songti SC", "Noto Serif CJK SC", "Source Han Serif SC",
             "AR PL UMing CN", "WenQuanYi Zen Hei"],
    "simsun": ["SimSun", "Songti SC", "Noto Serif CJK SC", "Source Han Serif SC"],
    "新宋体": ["NSimSun", "SimSun", "Noto Serif CJK SC"],
    "仿宋": ["FangSong", "STFangsong", "Noto Serif CJK SC", "AR PL UKai CN"],
    "fangsong": ["FangSong", "STFangsong", "Noto Serif CJK SC"],
    "仿宋_gb2312": ["FangSong", "STFangsong", "Noto Serif CJK SC"],
    "楷体": ["KaiTi", "STKaiti", "AR PL UKai CN", "Noto Serif CJK SC"],
    "kaiti": ["KaiTi", "STKaiti", "AR PL UKai CN"],
    "黑体": ["SimHei", "Heiti SC", "Noto Sans CJK SC", "Source Han Sans SC",
             "WenQuanYi Zen Hei", "DejaVu Sans"],
    "simhei": ["SimHei", "Heiti SC", "Noto Sans CJK SC"],
    "微软雅黑": ["Microsoft YaHei", "Noto Sans CJK SC", "WenQuanYi Micro Hei"],
    "microsoft yahei": ["Microsoft YaHei", "Noto Sans CJK SC"],
    "等线": ["DengXian", "Noto Sans CJK SC"],
    "方正书宋": ["FZShuSong", "Noto Serif CJK SC", "SimSun"],
    "方正黑体": ["FZHei", "Noto Sans CJK SC", "SimHei"],
    "方正小标宋": ["FZXiaoBiaoSong", "Noto Serif CJK SC", "SimSun"],
    "times new roman": ["times", "Times New Roman", "Liberation Serif", "DejaVu Serif",
                        "Noto Serif"],
    "arial": ["arial", "Arial", "Liberation Sans", "DejaVu Sans", "Noto Sans"],
    "helvetica": ["helvetica", "Helvetica", "Liberation Sans", "DejaVu Sans"],
    "courier": ["cour", "Courier New", "Liberation Mono", "DejaVu Sans Mono"],
    "courier new": ["cour", "Courier New", "Liberation Mono", "DejaVu Sans Mono"],
    "kaiti": ["simkai", "KaiTi", "STKaiti", "AR PL UKai CN", "Noto Serif CJK SC"],
    "songti": ["simsun", "Songti SC", "STSong", "Noto Serif CJK SC"],
    "heiti": ["simhei", "Heiti SC", "STHeiti", "Noto Sans CJK SC"],
    "dengxian": ["deng", "DengXian", "Noto Sans CJK SC"],
    "fangsong": ["simfang", "FangSong", "STFangsong", "Noto Serif CJK SC"],
    "yahei": ["msyh", "Microsoft YaHei", "Noto Sans CJK SC"],
    "consolas": ["consola", "Consolas", "Liberation Mono", "DejaVu Sans Mono"],
    "verdana": ["verdana", "Verdana", "DejaVu Sans"],
    "calibri": ["calibri", "Calibri", "Carlito", "DejaVu Sans"],
}

# 系统字体目录（麒麟/统信/Ubuntu 都在里面，Windows 也给上便于本地调试）
FONT_DIRS = [
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
    "/System/Library/Fonts",
    "/Library/Fonts",
    "C:\\Windows\\Fonts",
]

_FONT_EXTS = (".ttf", ".ttc", ".otf", ".otc", ".TTF", ".TTC", ".OTF")
_CJK_HINTS = ("cjk", "hei", "song", "kai", "fang", "ming", "yahei", "wqy",
              "noto", "source han", "ukai", "uming", "droid")

_cache: Optional[Dict[str, List[str]]] = None
_cache_paths: List[str] = []


def font_dirs() -> List[str]:
    dirs = list(FONT_DIRS)
    extra = os.environ.get("OFD_FONT_DIRS")
    if extra:
        dirs.extend(p for p in extra.split(os.pathsep) if p)
    return [d for d in dirs if os.path.isdir(d)]


def scan(force: bool = False) -> Dict[str, List[str]]:
    """扫描系统字体，返回归一化 family 名 → 文件列表。"""
    global _cache, _cache_paths
    if _cache is not None and not force:
        return _cache
    index: Dict[str, List[str]] = {}
    files: List[str] = []
    for d in font_dirs():
        for path in glob.iglob(os.path.join(d, "**", "*"), recursive=True):
            if not path.lower().endswith(tuple(e.lower() for e in _FONT_EXTS)):
                continue
            files.append(path)
            for name in _names_for(path):
                index.setdefault(name, []).append(path)
    _cache = index
    _cache_paths = files
    return index


def _names_for(path: str) -> List[str]:
    """从一个文件名推出若干可匹配的 family 名（不解析字体表，够用且快）。"""
    base = os.path.splitext(os.path.basename(path))[0]
    out = set()
    out.add(_norm(base))
    # 去掉 -Regular / _Bold / [style] 之类的尾巴
    cleaned = re.sub(r"[-_\[\(].*$", "", base)
    out.add(_norm(cleaned))
    # NotoSansCJK-Regular → Noto Sans CJK
    pretty = re.sub(r"([a-z])([A-Z])", r"\1 \2", cleaned)
    pretty = re.sub(r"[-_]+", " ", pretty)
    out.add(_norm(pretty))
    return [n for n in out if n]


def _norm(name: str) -> str:
    s = (name or "").strip().lower()
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def all_files() -> List[str]:
    scan()
    return list(_cache_paths)


def cjk_font() -> Optional[str]:
    """挑一个能显示中文的字体文件。"""
    index = scan()
    prefer = ["noto sans cjk sc", "noto serif cjk sc", "source han sans sc",
              "source han serif sc", "wenquanyi zen hei", "wenquanyi micro hei",
              "droid sans fallback", "ar pl uming cn", "simsun", "simhei",
              "microsoft yahei", "pingfang sc", "heiti sc", "songti sc"]
    for p in prefer:
        if p in index and index[p]:
            return _pick_regular(index[p])
    # 退一步：文件名里带 cjk 的
    for name, paths in index.items():
        if any(h in name for h in _CJK_HINTS) and paths:
            return _pick_regular(paths)
    files = all_files()
    return files[0] if files else None


MONO_SUBSTITUTES = [
    "Liberation Mono", "DejaVu Sans Mono", "Nimbus Mono PS", "Courier New",
    "Consolas", "Noto Sans Mono CJK SC", "WenQuanYi Zen Hei Mono",
]


def resolve(family: str, bold: bool = False, italic: bool = False,
            embedded: Optional[str] = None, mono: bool = False) -> Optional[str]:
    """按 OFD 里的字体名选一个本机字体文件。embedded 是包内嵌入字体路径。

    mono 来自表44 的 FixedWidth，标准原文是"用于匹配替代字型"：
    原名能找到就照用，找不到才优先等宽字体。它不改变排版步进。
    """
    if embedded and os.path.exists(embedded):
        return embedded
    index = scan()
    key = _norm(family)
    candidates: List[str] = []

    # 1. 精确同名
    if key in index:
        candidates.extend(index[key])
    # 2. 中文名 → 文件名前缀（楷体 → simkai，微软雅黑 → msyh）
    hint = FILE_HINTS.get((family or "").strip()) or FILE_HINTS.get(key)
    if hint:
        for h in hint:
            k = _norm(h)
            if k in index and index[k]:
                return _prefer_style(index[k], bold, italic)
    # 3. 别名表
    alias = ALIASES.get((family or "").strip().lower()) or ALIASES.get(key)
    if alias:
        for a in alias:
            k = _norm(a)
            if k in index:
                candidates.extend(index[k])
    # 4. 包含匹配（"方正书宋简体" 命中 "fzshusong"）
    if not candidates and key:
        for name, paths in index.items():
            if key in name or name in key:
                candidates.extend(paths)
                break
    # 4.5 表44 FixedWidth：替代时优先等宽字型
    if not candidates and mono:
        for name in MONO_SUBSTITUTES:
            k = _norm(name)
            if k in index and index[k]:
                candidates.extend(index[k])
                break
    # 5. 中文兜底
    if not candidates:
        cjk = cjk_font()
        if cjk:
            return cjk
    if not candidates:
        return None
    return _prefer_style(candidates, bold, italic)


def _prefer_style(paths: List[str], bold: bool, italic: bool) -> str:
    want = []
    if bold:
        want.extend(["bold", "bd", "heavy", "semibold", "demi"])
    if italic:
        want.extend(["italic", "oblique", "it"])
    if want:
        for p in paths:
            low = p.lower()
            if any(w in low for w in want):
                return p
    return _pick_regular(paths)


def _pick_regular(paths: List[str]) -> str:
    for p in paths:
        low = p.lower()
        if any(t in low for t in ("regular", "normal", "book", "roman")):
            return p
    return paths[0]


def describe() -> dict:
    """给界面显示用：字体目录、可用字体数、中文字体。"""
    index = scan()
    return {
        "dirs": font_dirs(),
        "families": len(index),
        "files": len(all_files()),
        "cjk": cjk_font(),
    }

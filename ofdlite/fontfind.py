# -*- coding: utf-8 -*-
"""字体查找：把 OFD 里的 FontName / FamilyName 映射到本机字体文件。

OFD 的字体引用可能是「内嵌 TTF」（Res/Font_N.ttf）或「系统字体名」
（如楷体 / SimSun / Courier New）。真实数电发票用的是后者，包里没有字体文件。

做法：扫系统字体目录，读每个字体的 name 表拿家族名与 PostScript 名，
建立「小写名字 → 文件」索引；查不到时按内置别名表回退，
最后兜底到全覆盖中文字体。

不依赖 fontconfig：它的输出是给人看的字符串，解析起来脆；
直接读 TTF 的 name 表更确定，且与 ttf.py 共用同一套读法。
"""
from __future__ import annotations

import os
import struct
from typing import Dict, List, Optional, Tuple

# Linux / 麒麟 / Windows 上常见的字体目录
FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
    "/usr/share/fonts/truetype",
    "C:/Windows/Fonts",
)

# 常见别名：OFD 里出现的名字 → 可能的本机字体名（按优先级）
ALIASES = {
    "宋体": ["SimSun", "宋体", "Noto Serif CJK SC", "Source Han Serif SC",
             "WenQuanYi Zen Hei", "Droid Sans Fallback", "文泉驿正黑"],
    "simsun": ["SimSun", "宋体", "Noto Serif CJK SC", "Droid Sans Fallback"],
    "黑体": ["SimHei", "黑体", "Noto Sans CJK SC", "Source Han Sans SC",
             "WenQuanYi Micro Hei", "Droid Sans Fallback", "文泉驿微米黑"],
    "simhei": ["SimHei", "黑体", "Noto Sans CJK SC", "WenQuanYi Micro Hei",
               "Droid Sans Fallback"],
    "楷体": ["KaiTi", "楷体", "AR PL UKai CN", "Noto Serif CJK SC",
             "Droid Sans Fallback", "WenQuanYi Zen Hei"],
    "kaiti": ["KaiTi", "楷体", "AR PL UKai CN", "Droid Sans Fallback"],
    "仿宋": ["FangSong", "仿宋", "AR PL UMing CN", "Noto Serif CJK SC",
             "Droid Sans Fallback"],
    "courier new": ["Courier New", "Nimbus Mono PS", "Liberation Mono",
                    "DejaVu Sans Mono", "Courier"],
    "times new roman": ["Times New Roman", "Nimbus Roman", "Liberation Serif",
                        "DejaVu Serif"],
    "arial": ["Arial", "Liberation Sans", "DejaVu Sans", "Nimbus Sans"],
}

# 兜底：覆盖中日韩的字体，按优先级
FALLBACK_CJK = (
    "Droid Sans Fallback",
    "WenQuanYi Micro Hei",
    "WenQuanYi Zen Hei",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
)

_index: Optional[Dict[str, str]] = None
_cache: Dict[str, Optional[str]] = {}


def _read_name_table(path: str) -> Tuple[List[str], Optional[str]]:
    """读 TTF/TTC 的 name 表，返回 (家族名候选小写列表, 是否 TTC)。"""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if len(head) < 12:
                return [], None
            tag = head[:4]
            if tag == b"ttcf":
                # 字体集合：取第一个子字体的表目录
                if len(head) < 16:
                    return [], "ttc"
                off = struct.unpack(">I", head[12:16])[0]
                f.seek(off)
                head = f.read(12)
                if len(head) < 12:
                    return [], "ttc"
            elif tag not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
                return [], None
            num = struct.unpack(">H", head[4:6])[0]
            f.seek(off + 12 if tag == b"ttcf" else 12)
            dirs = f.read(num * 16)
            name_off = None
            for i in range(num):
                rec = dirs[i * 16:(i + 1) * 16]
                if len(rec) < 16:
                    break
                t = rec[:4]
                if t == b"name":
                    name_off = struct.unpack(">I", rec[8:12])[0]
                    break
            if name_off is None:
                return [], "ttc" if tag == b"ttcf" else None
            f.seek(name_off)
            hdr = f.read(6)
            if len(hdr) < 6:
                return [], None
            _fmt, count, str_off = struct.unpack(">HHH", hdr)
            recs = f.read(count * 12)
            names = []
            for i in range(count):
                r = recs[i * 12:(i + 1) * 12]
                if len(r) < 12:
                    break
                pid, eid, lid, nid, ln, off2 = struct.unpack(">HHHHHH", r)
                if nid not in (1, 4, 6):        # 家族 / 全名 / PostScript 名
                    continue
                f.seek(name_off + str_off + off2)
                raw = f.read(ln)
                try:
                    if pid == 3 or pid == 0:
                        s = raw.decode("utf-16-be", "ignore")
                    else:
                        s = raw.decode("latin-1", "ignore")
                except Exception:
                    continue
                s = s.strip()
                if s:
                    names.append(s.lower())
            return names, "ttc" if tag == b"ttcf" else None
    except Exception:
        return [], None


def build_index(force: bool = False) -> Dict[str, str]:
    """扫字体目录，建立「小写名字 → 文件路径」索引。"""
    global _index
    if _index is not None and not force:
        return _index
    idx: Dict[str, str] = {}
    for d in FONT_DIRS:
        if not os.path.isdir(d):
            continue
        for base, _dirs, files in os.walk(d):
            for fn in files:
                # 三种都收：ttf/ttc 是 glyf 轮廓，otf 可能是 CFF
                if not fn.lower().endswith((".ttf", ".ttc", ".otf")):
                    continue
                p = os.path.join(base, fn)
                names, _kind = _read_name_table(p)
                # 文件名本身也当名字（方便按 simsun.ttc 这类命中）
                stem = os.path.splitext(fn)[0].lower()
                # 斜体/粗体变体不要抢占 Regular 的位置：
                # 同一家族名可能对应多个文件，先到先得会把 Italic 选进来
                # （实测 Courier New 被匹配成 NimbusMonoPS-Italic）。
                variants = ("italic", "oblique", "bold", "-bd", "-bi", "-it")
                is_variant = any(v in fn.lower() for v in variants) or any(
                    any(v in n for v in variants) for n in names)
                for key in names + [stem]:
                    if is_variant and key in idx:
                        continue
                    if is_variant:
                        idx.setdefault("~" + key, p)     # 变体放冷宫
                    else:
                        idx.setdefault(key, p)
    _index = idx
    return idx


MONO_SUBSTITUTES = [
    "Liberation Mono", "DejaVu Sans Mono", "Nimbus Mono PS", "Courier New",
    "Consolas", "Noto Sans Mono CJK SC", "WenQuanYi Zen Hei Mono",
]


def find(font_name: str, mono: bool = False) -> Optional[str]:
    """把 OFD 里的字体名解析成文件路径。查不到返回 None。

    mono=True 来自表44 的 FixedWidth。标准原文那句是"用于匹配替代字型"——
    它只影响**替代**顺序：原名能命中就用原名，找不到时优先等宽字体。
    它不改变排版步进（把每个字形都按 em 算是对表44 的误读，见附三十九）。
    """
    if not font_name:
        font_name = ""
    key = font_name.strip().lower()
    ck = key + ("|mono" if mono else "")
    if ck in _cache:
        return _cache[ck]
    idx = build_index()
    hit = None
    # 1) 直接命中
    if key in idx:
        hit = idx[key]
    # 2) 去空格/连字符再试
    if hit is None:
        compact = key.replace(" ", "").replace("-", "")
        for k, v in idx.items():
            if k.replace(" ", "").replace("-", "") == compact:
                hit = v
                break
    # 3) 别名表
    if hit is None:
        for cand in ALIASES.get(key, []):
            c = cand.lower()
            if c in idx:
                hit = idx[c]
                break
            compact = c.replace(" ", "")
            for k, v in idx.items():
                if k.replace(" ", "") == compact:
                    hit = v
                    break
            if hit:
                break
    # 3.5) 表44 FixedWidth：替代时优先等宽字体
    if hit is None and mono:
        for cand in MONO_SUBSTITUTES:
            c = cand.lower()
            if c in idx:
                hit = idx[c]
                break
    # 4) 兜底到全覆盖中文字体
    if hit is None:
        for cand in FALLBACK_CJK:
            c = cand.lower()
            if c in idx:
                hit = idx[c]
                break
    _cache[ck] = hit
    return hit


def describe() -> str:
    idx = build_index()
    return "字体索引 %d 条" % len(idx)

# -*- coding: utf-8 -*-
"""打印：CUPS 命令生成、拼版（N 合 1）、双面、小册子。

两条路径：
  1. 桌面端走 Qt QPrinter（矢量、能预览、能直接打）——见 app/print_dialog.py
  2. 命令行/网页端走 CUPS：先把选定页导出成 PDF，再调 lp
     拼版（2/4/6/9 合 1）在这里用 Pillow 合成一页多图，交给 CUPS 打整页。

lp 的参数按 CUPS 通用选项写，麒麟自带的 CUPS 1.2+ 都认：
  -n 份数  -o media=A4  -o sides=two-sided-long-edge  -o fit-to-page
  -o number-up=N（部分驱动不认，所以拼版默认自己做，不用这个开关）
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Sequence

from . import model
from .errors import PrintError

PAPER_MM = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A5": (148.0, 210.0),
    "Letter": (215.9, 279.4),
    "Legal": (215.9, 355.6),
    "B5": (176.0, 250.0),
    "16K": (184.0, 260.0),
}


def paper_size(name: str):
    return PAPER_MM.get((name or "A4").strip(), PAPER_MM["A4"])


# ------------------------------------------------------------------ 打印机

def list_printers() -> List[Dict[str, str]]:
    """列出 CUPS 打印机。返回 [{name, info, location, accepting}]。"""
    lpstat = shutil.which("lpstat")
    if not lpstat:
        return []
    out = []
    try:
        res = subprocess.run([lpstat, "-a"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    accepting = set()
    for line in res.stdout.splitlines():
        # "Office accepting requests since ..."
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "accepting":
            accepting.add(parts[0].rstrip(":"))
    try:
        res2 = subprocess.run([lpstat, "-p"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        res2 = None
    names = accepting or set()
    if res2 is not None:
        cur = None
        for line in res2.stdout.splitlines():
            if line.startswith("printer "):
                cur = line.split()[1]
                names.add(cur)
                out.append({"name": cur, "info": line.strip(), "location": "",
                            "accepting": "true" if cur in accepting else "unknown"})
            elif cur and line.strip().startswith("Description:"):
                for e in out:
                    if e["name"] == cur:
                        e["info"] = line.split(":", 1)[1].strip()
    if not out:
        out = [{"name": n, "info": "", "location": "",
                "accepting": "true" if n in accepting else "unknown"} for n in sorted(names)]
    return out


def default_printer() -> Optional[str]:
    lpstat = shutil.which("lpstat")
    if not lpstat:
        return None
    try:
        res = subprocess.run([lpstat, "-d"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    line = res.stdout.strip()
    if ":" in line:
        return line.split(":", 1)[1].strip()
    return None


# ------------------------------------------------------------------ 拼版规划

def plan_sheets(page_count: int, n_up: int = 1, duplex: bool = False) -> List[List[Optional[int]]]:
    """把 N 页排到若干张纸上。

    返回每张纸的全部页位（0 基页号，None 是空位）。
    一张纸容量 = n_up × (2 if duplex else 1)。
    """
    n_up = max(1, int(n_up))
    per_sheet = n_up * (2 if duplex else 1)
    sheets = []
    for start in range(0, max(1, page_count), per_sheet):
        slot = []
        for k in range(per_sheet):
            idx = start + k
            slot.append(idx if idx < page_count else None)
        sheets.append(slot)
    return sheets


def booklet_order(page_count: int) -> List[Optional[int]]:
    """小册子拼版顺序（双面、骑马钉）。

    4 页 → [3, 0, 1, 2]：第 1 面是 4|1，第 2 面是 2|3。
    总页数补到 4 的倍数，空位为 None。
    """
    n = page_count
    while n % 4:
        n += 1
    order = []
    for i in range(n // 2):
        left = n - 1 - i
        right = i
        if i % 2 == 0:
            order.append(left)
            order.append(right)
        else:
            order.append(right)
            order.append(left)
    return [p if p < page_count else None for p in order]


# ------------------------------------------------------------------ 拼接图

def compose_nup(pkg: model.OFDPackage, pages: Sequence[int], n_up: int,
                paper: str = "A4", dpi: int = 150, margin_mm: float = 10.0,
                gap_mm: float = 4.0, duplex: bool = False,
                booklet: bool = False, landscape: bool = True) -> bytes:
    """把多页拼到一张纸上，输出 PDF 字节。

    n_up=1 时等于原样导出；n_up>1 时按网格排布，保持每页比例居中。
    """
    from PIL import Image
    from .render_pil import PilRenderer
    from .scene import build_page_scene

    all_pages = pkg.all_pages()
    idx = [i for i in pages if 0 <= i < len(all_pages)]
    if not idx:
        raise PrintError("没有选中要打印的页面")

    seq: List[Optional[int]]
    if booklet:
        seq = booklet_order(len(idx))
        seq = [idx[i] if i is not None else None for i in seq]
        n_up = 2
    else:
        seq = list(idx)
        if duplex:
            per = max(1, n_up) * 2
            while len(seq) % per:
                seq.append(None)

    pw, ph = paper_size(paper)
    if landscape and n_up > 1:
        pw, ph = ph, pw

    sheets = plan_sheets(len(seq), n_up=n_up, duplex=duplex) if not booklet else []
    if booklet:
        # 小册子：每面 2 页，每张纸正反两面
        chunks = []
        for i in range(0, len(seq), 4):
            chunks.append(seq[i:i + 4])
        sheets = chunks
    else:
        sheets = [seq[i:i + (n_up * (2 if duplex else 1))]
                  for i in range(0, len(seq), n_up * (2 if duplex else 1))]

    grid = _grid_for(n_up)
    cols, rows = grid
    margin = margin_mm
    cell_w = (pw - 2 * margin - gap_mm * (cols - 1)) / cols
    cell_h = (ph - 2 * margin - gap_mm * (rows - 1)) / rows

    out_imgs = []
    for slot in sheets:
        faces = slot if duplex else slot
        # 双面：一张纸的页位按"正面 n_up，背面 n_up"切分，输出成两张单页
        groups = []
        if duplex:
            groups.append(faces[:n_up])
            groups.append(faces[n_up:n_up * 2])
        else:
            groups.append(faces[:n_up])
        for group in groups:
            sheet = Image.new("RGB", (int(round(pw * dpi / 25.4)),
                                      int(round(ph * dpi / 25.4))), (255, 255, 255))
            for k, pidx in enumerate(group):
                if pidx is None:
                    continue
                r, c = divmod(k, cols)
                cell_x = margin + c * (cell_w + gap_mm)
                cell_y = margin + r * (cell_h + gap_mm)
                page = all_pages[pidx]
                sc = build_page_scene(page)
                re = PilRenderer(dpi=dpi)
                re.load_resources(sc.resources)
                pim = re.render(sc).convert("RGB")
                scale = min(cell_w / max(1e-6, sc.size.w), cell_h / max(1e-6, sc.size.h))
                tw = max(1, int(round(sc.size.w * scale * dpi / 25.4)))
                th = max(1, int(round(sc.size.h * scale * dpi / 25.4)))
                pim = pim.resize((tw, th), Image.LANCZOS)
                ox = int(round((cell_x + (cell_w - sc.size.w * scale) / 2) * dpi / 25.4))
                oy = int(round((cell_y + (cell_h - sc.size.h * scale) / 2) * dpi / 25.4))
                sheet.paste(pim, (max(0, ox), max(0, oy)))
            out_imgs.append(sheet)

    buf = io.BytesIO()
    first, rest = out_imgs[0], out_imgs[1:]
    first.save(buf, format="PDF", resolution=float(dpi), save_all=True,
               append_images=rest, title=pkg.document.title or "OFD")
    return buf.getvalue()


def _grid_for(n_up: int):
    return {1: (1, 1), 2: (2, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3)}.get(int(n_up), (1, 1))


# ------------------------------------------------------------------ CUPS

def build_lp_command(files: Sequence[str], printer: Optional[str] = None,
                     copies: int = 1, media: str = "A4",
                     duplex: str = "none", fit: bool = True,
                     pages: Optional[str] = None,
                     extra: Optional[Sequence[str]] = None) -> List[str]:
    """构造 lp 命令行。files 是 PDF 路径。"""
    lp = shutil.which("lp") or "lp"
    cmd = [lp]
    if printer:
        cmd += ["-d", str(printer)]
    if copies and copies > 1:
        cmd += ["-n", str(int(copies))]
    if media:
        cmd += ["-o", "media=%s" % media]
    if duplex in ("long-edge", "short-edge"):
        cmd += ["-o", "sides=two-sided-%s" % duplex]
    else:
        cmd += ["-o", "sides=one-sided"]
    if fit:
        cmd += ["-o", "fit-to-page"]
    if pages:
        cmd += ["-P", str(pages)]
    for e in (extra or []):
        cmd += ["-o", str(e)]
    cmd += list(files)
    return cmd


def submit(cmd: Sequence[str], timeout: int = 60) -> Dict[str, object]:
    """执行打印命令，返回 {ok, code, stdout, stderr, cmd}。"""
    try:
        res = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise PrintError("系统里找不到 lp（cups-client 未安装）")
    except subprocess.SubprocessError as exc:
        raise PrintError("调用 lp 失败: %s" % exc)
    return {
        "ok": res.returncode == 0,
        "code": res.returncode,
        "stdout": res.stdout.strip(),
        "stderr": res.stderr.strip(),
        "cmd": " ".join(cmd),
    }


def print_pdf_file(pdf_path: str, printer: Optional[str] = None, copies: int = 1,
                   media: str = "A4", duplex: str = "none", fit: bool = True,
                   pages: Optional[str] = None) -> Dict[str, object]:
    if not os.path.exists(pdf_path):
        raise PrintError("要打印的 PDF 不存在: %s" % pdf_path)
    cmd = build_lp_command([pdf_path], printer, copies, media, duplex, fit, pages)
    return submit(cmd)


def print_document(pkg: model.OFDPackage, printer: Optional[str] = None,
                   pages: Optional[Sequence[int]] = None, copies: int = 1,
                   n_up: int = 1, paper: str = "A4", duplex: bool = False,
                   booklet: bool = False, dpi: int = 200,
                   keep_pdf: Optional[str] = None) -> Dict[str, object]:
    """一步打印：拼版 → PDF → lp。"""
    all_pages = pkg.all_pages()
    idx = list(range(len(all_pages))) if pages is None else [i for i in pages if 0 <= i < len(all_pages)]
    if not idx:
        raise PrintError("没有选中要打印的页面")

    if n_up == 1 and not booklet and not duplex:
        from .convert import export_pdf
        pdf_path = keep_pdf or os.path.join(tempfile.mkdtemp(prefix="ofdprint-"), "print.pdf")
        export_pdf(pkg, pdf_path, dpi=dpi, pages=idx)
    else:
        data = compose_nup(pkg, idx, n_up, paper=paper, dpi=dpi,
                           duplex=duplex, booklet=booklet)
        pdf_path = keep_pdf or os.path.join(tempfile.mkdtemp(prefix="ofdprint-"), "print.pdf")
        with open(pdf_path, "wb") as fh:
            fh.write(data)

    sides = "long-edge" if duplex else "none"
    res = print_pdf_file(pdf_path, printer=printer, copies=copies,
                         media=paper, duplex=sides, fit=True)
    res["pdf"] = pdf_path
    return res


def cups_available() -> bool:
    return bool(shutil.which("lp") or shutil.which("lpr"))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Python 依赖（Pillow / pycairo）连同它们的 .so 依赖收进 vendor/。

目的：deb 不再依赖系统的 python3-pil / python3-cairo，装到没网的麒麟机器上也能跑。

做法：
  1. 找到模块目录与它的 C 扩展
  2. 用 ldd 递归收集 .so 依赖
  3. 排除**绝对不能自带**的（glibc 系列）与**必须跟随系统**的（字体配置）
  4. 拷进 vendor/python/（模块）与 vendor/lib/（.so），保留 soname 符号链

产物：
  vendor/python/<模块>/           ← PYTHONPATH 指向这里
  vendor/lib/*.so*                ← LD_LIBRARY_PATH 指向这里
  vendor/MANIFEST.txt             ← 收集了哪些文件、来自哪个包、许可
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VENDOR = os.path.join(ROOT, "vendor")

# 绝不能自带：glibc 全家。带了会和目标机系统冲突，且版本不匹配会直接崩。
GLIBC = (
    "libc.so", "libm.so", "libpthread.so", "libdl.so", "librt.so",
    "libresolv.so", "libutil.so", "libnsl.so", "libanl.so",
    "ld-linux", "libcrypt.so",
)

# 建议跟随系统：字体发现必须读系统 fontconfig 配置，自带会看不到系统字体。
FOLLOW_SYSTEM = (
    "libfontconfig.so", "libfreetype.so",
)

# 要收集的 Python 模块（模块名, import 名）
MODULES = (
    ("Pillow", "PIL"),
    ("pycairo", "cairo"),
)


def run(cmd):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode(
            "utf-8", "replace")
    except (OSError, subprocess.CalledProcessError):
        return ""


def module_path(imp: str):
    out = run([sys.executable, "-c",
               "import %s, os; print(os.path.dirname(%s.__file__))" % (imp, imp)])
    p = out.strip()
    return p if p and os.path.isdir(p) else None


LIB_SEARCH = (
    "/usr/lib/x86_64-linux-gnu",
    "/lib/x86_64-linux-gnu",
    "/usr/lib64",
    "/lib64",
    "/usr/lib",
    "/lib",
    "/usr/local/lib",
)


def find_lib(name: str):
    """按库名在系统目录里找实际文件（跟着符号链接到真实文件）。"""
    for d in LIB_SEARCH:
        p = os.path.join(d, name)
        if os.path.exists(p):
            real = os.path.realpath(p)
            return real if os.path.exists(real) else p
    # 有的发行版把带 hash 的名字放在别的目录，退回 ldconfig 缓存查
    out = run(["ldconfig", "-p"])
    for line in out.splitlines():
        m = re.match(r"\s*(\S+)\s+\(libc6\)\s+=>\s+(\S+)", line)
        if m and m.group(1) == name:
            return m.group(2)
    return None


def so_deps(so_path: str):
    """返回 [(needed_name, 实际文件路径)]。

    不用 ldd：它在 vendor 这种没有系统路径的目录里会对很多库报 not found，
    导致依赖收集器直接拿到空列表（实测 PIL 的 _imaging.so 全报 not found）。
    readelf -d 只读 ELF 头里的 DT_NEEDED，不受运行环境影响。
    """
    out = run(["readelf", "-d", so_path])
    res = []
    for line in out.splitlines():
        if "(NEEDED)" not in line:
            continue
        m = re.search(r"\[([^\]]+)\]", line)
        if not m:
            continue
        name = m.group(1)
        path = find_lib(name)
        res.append((name, path))
    return res


def is_glibc(soname: str) -> bool:
    return any(soname.startswith(g) for g in GLIBC)


def is_system_font(soname: str) -> bool:
    return any(soname.startswith(f) for f in FOLLOW_SYSTEM)


def collect(start_files):
    """从一批 .so 出发 BFS 收集依赖；返回 (要拷的清单, 跳过的清单, 找不到的清单)。"""
    want, skip, missing = {}, {}, {}
    queue = list(start_files)
    seen = set()
    while queue:
        cur = queue.pop(0)
        real = os.path.realpath(cur)
        if real in seen:
            continue
        seen.add(real)
        for name, path in so_deps(cur):
            if name in want or name in skip or name in missing:
                continue
            if is_glibc(name):
                skip[name] = "glibc 系列，不能自带"
                continue
            if is_system_font(name):
                skip[name] = "跟随系统（字体配置）"
                continue
            if not path:
                missing[name] = "系统里找不到这个库"
                continue
            want[name] = path
            queue.append(path)
    return want, skip, missing


def main():
    if os.path.isdir(VENDOR):
        shutil.rmtree(VENDOR)
    py_dir = os.path.join(VENDOR, "python")
    lib_dir = os.path.join(VENDOR, "lib")
    os.makedirs(py_dir, exist_ok=True)
    os.makedirs(lib_dir, exist_ok=True)

    manifest = []
    so_starts = []
    got_modules = []

    for pkg_name, imp in MODULES:
        src = module_path(imp)
        if not src:
            print("  跳过 %s（没装）" % pkg_name)
            continue
        dst = os.path.join(py_dir, os.path.basename(src))
        shutil.copytree(src, dst, symlinks=True)
        # pip 装的 manylinux wheel 把依赖库放在同级的 <名字>.libs/ 里，
        # C 扩展的 RPATH 是 $ORIGIN/../<名字>.libs —— 一起拷过去，
        # RPATH 自己就能解析，不必逐个收集（实测 Pillow 的 18 个库全在这）。
        # .libs 目录名不一定等于模块名（Pillow 的模块是 PIL，库目录是 pillow.libs），
        # 所以扫同级所有 *.libs 一起拷，别靠猜名字。
        parent = os.path.dirname(src)
        try:
            entries = os.listdir(parent)
        except OSError:
            entries = []
        for ent in entries:
            if not ent.endswith(".libs"):
                continue
            full = os.path.join(parent, ent)
            if not os.path.isdir(full):
                continue
            libs_dst = os.path.join(py_dir, ent)
            if os.path.isdir(libs_dst):
                continue
            shutil.copytree(full, libs_dst, symlinks=True)
            ssize = sum(os.path.getsize(os.path.join(b, f))
                        for b, _d, fs in os.walk(libs_dst) for f in fs)
            print("  wheel 库 %-14s %6.1f MB  %s" % (ent, ssize / 1048576.0, libs_dst))
            manifest.append("wheel库\t%s\t%s\t%.1f MB" % (
                ent, full, ssize / 1048576.0))
        got_modules.append((pkg_name, imp, src))
        # 收集该模块下的所有 .so 作为依赖起点
        for base, _dirs, files in os.walk(dst):
            for fn in files:
                if fn.endswith(".so") or ".so." in fn:
                    so_starts.append(os.path.join(base, fn))
        size = sum(os.path.getsize(os.path.join(b, f))
                   for b, _d, fs in os.walk(dst) for f in fs)
        print("  模块 %-10s %6.1f MB  %s" % (pkg_name, size / 1048576.0, dst))
        manifest.append("模块\t%s\t%s\t%.1f MB" % (pkg_name, src, size / 1048576.0))

    if not so_starts:
        print("没有任何可收集的 .so")
        return 1

    want, skip, missing = collect(so_starts)
    total = 0
    for name, path in sorted(want.items()):
        # 以 **DT_NEEDED 里的名字**落盘：运行期正是按这个名字找的。
        # Ubuntu 的 symbol versioning 下它就是 libtiff-20b7e810.so.6.3.0
        # 这种带 hash 的全名，直接用它当文件名最省事，不用再建链。
        real = os.path.realpath(path)
        dst = os.path.join(lib_dir, name)
        if not os.path.exists(dst):
            shutil.copy2(real, dst)
            total += os.path.getsize(real)
            manifest.append("库\t%s\t%s\t%d KB" % (name, real,
                                                     os.path.getsize(real) // 1024))

    # 补齐 DT_NEEDED 的链接（Ubuntu 的 hash 命名）
    print()
    print("  自带 .so %d 个，%.1f MB" % (len(want), total / 1048576.0))
    if skip:
        print("  未自带 %d 个：" % len(skip))
        for soname, why in sorted(skip.items()):
            print("    %-28s %s" % (soname, why))
    if missing:
        print("  找不到 %d 个（需人工确认）：" % len(missing))
        for soname, why in sorted(missing.items()):
            print("    %-28s %s" % (soname, why))

    with open(os.path.join(VENDOR, "MANIFEST.txt"), "w", encoding="utf-8") as f:
        f.write("xiaohuang-ofd 自带依赖清单\n")
        f.write("生成工具：tools/vendor_deps.py\n\n")
        for line in manifest:
            f.write(line + "\n")
        f.write("\n跳过的（必须由系统提供）：\n")
        for soname, why in sorted(skip.items()):
            f.write("  %-28s %s\n" % (soname, why))

    # 许可文本一起带上（LGPL/MPL/HPND 都要求随分发保留）
    lic_dir = os.path.join(VENDOR, "licenses")
    os.makedirs(lic_dir, exist_ok=True)
    for name, paths in (
        ("LGPL-2.1", ["/usr/share/common-licenses/LGPL-2.1"]),
        ("MPL-1.1", ["/usr/share/common-licenses/MPL-1.1"]),
        ("Pillow-LICENSE", ["/usr/lib/python3/dist-packages/PIL-*.dist-info/LICENSE",
                            "/usr/share/doc/python3-pil/copyright"]),
    ):
        for p in paths:
            import glob
            hits = glob.glob(p)
            if hits:
                shutil.copy2(hits[0], os.path.join(lic_dir, name + ".txt"))
                break
    print()
    print("  许可文本：%s" % os.path.relpath(lic_dir, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())

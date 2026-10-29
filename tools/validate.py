# -*- coding: utf-8 -*-
"""OFD 结构校验器：拿 GB/T 33190 附录 A 的 XSD 检查包内 XML。

    python tools/validate.py 文件.ofd
    python tools/validate.py 文件.ofd --strict      # 有 fail 时退出码 1
    python tools/validate.py --list-schemas

XSD 要先从标准里抽出来：

    python tools/extract_xsd.py
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofdlite import OFDPackage
from ofdlite.schema import SchemaSet, schema_for_entry


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="要校验的 .ofd")
    ap.add_argument("--xsd-dir", default=None, help="XSD 所在目录（默认 docs/xsd）")
    ap.add_argument("--strict", action="store_true", help="有不符合项时退出码 1")
    ap.add_argument("--list-schemas", action="store_true", help="列出可用 schema")
    ap.add_argument("--verbose", action="store_true", help="列出每个条目")
    args = ap.parse_args(argv)

    ss = SchemaSet(args.xsd_dir)
    if args.list_schemas:
        names = ss.known_schemas()
        print("XSD 目录:", ss.xsd_dir)
        print("可用 schema (%d):" % len(names))
        for n in names:
            print("  -", n)
        return 0

    if not ss.is_available():
        print("校验不可用:", ss.unavailable_reason())
        return 2
    if not args.files:
        ap.print_help()
        return 2

    total_bad = 0
    for path in args.files:
        if not os.path.isfile(path):
            print("找不到文件:", path)
            total_bad += 1
            continue
        print("=" * 66)
        print("文件:", os.path.basename(path))
        try:
            pkg = OFDPackage.open(path)
        except Exception as exc:
            print("  打不开:", exc)
            total_bad += 1
            continue
        report = ss.validate_package(pkg)
        if not report:
            print("  包内没有可校验的条目")
            continue
        ok, bad, skip = ss.summary(report)
        for name, (status, errs) in sorted(report.items()):
            sch = schema_for_entry(name)
            if args.verbose or status != "ok":
                mark = {"ok": "ok  ", "fail": "FAIL", "unavailable": "skip"}[status]
                print("  %s %-46s [%s]" % (mark, name, sch))
                for e in errs:
                    print("        ", e)
        print("  通过 %d / 失败 %d / 跳过 %d（共 %d 个条目）"
              % (ok, bad, skip, len(report)))
        total_bad += bad

    print()
    print("总体:", "全部通过" if total_bad == 0 else "%d 处不符合" % total_bad)
    return 0 if (total_bad == 0 or not args.strict) else 1


if __name__ == "__main__":
    sys.exit(main())

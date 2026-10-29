# -*- coding: utf-8 -*-
"""OFD 结构校验：用 GB/T 33190 附录 A 的 XSD 检查包内 XML。

XSD 不在包里，先跑一次抽取：

    python tools/extract_xsd.py        # 从 docs/GBT_33190-2016.txt 抽到 docs/xsd/

依赖 lxml（可选）。没装时 is_available() 返回 False，调用方据此跳过校验，
不要把它当成"文件没问题"。

用法：

    from ofdlite.schema import SchemaSet
    ss = SchemaSet()
    if ss.is_available():
        report = ss.validate_package(pkg)
        for name, errs in report.items():
            ...
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_XSD_DIR = os.path.join(os.path.dirname(_HERE), "docs", "xsd")

# 包内条目 → 该条目应该符合的 schema（附录 A.1~A.13）
def schema_for_entry(name: str) -> Optional[str]:
    n = name.replace("\\", "/")
    base = n.rsplit("/", 1)[-1]
    if base == "OFD.xml":
        return "OFD.xsd"
    if base == "Document.xml":
        return "Document.xsd"
    if base == "Content.xml":
        # 页内容与模板页内容结构相同，都用 Page.xsd
        return "Page.xsd"
    if base in ("PublicRes.xml", "DocumentRes.xml", "PageRes.xml", "Res.xml"):
        return "Res.xsd"
    if base == "Annotations.xml":
        return "Annotations.xsd"
    if re.search(r"Annots/.*\.xml$", n) and base != "Annotations.xml":
        return "Annotation.xsd"
    if base == "Signatures.xml":
        return "Signatures.xsd"
    if re.search(r"Signs/.*Signatures?\.xml$", n):
        return "Signatures.xsd"
    if base == "CustomTags.xml":
        return "CustomTags.xsd"
    if base == "Extensions.xml":
        return "Extensions.xsd"
    if base == "Attachments.xml":
        return "Attachments.xsd"
    if base == "Versions.xml":
        return "Version.xsd"
    return None


class SchemaSet(object):
    """按需编译 schema，缓存复用。"""

    def __init__(self, xsd_dir: Optional[str] = None):
        self.xsd_dir = xsd_dir or _DEFAULT_XSD_DIR
        self._schemas: Dict[str, object] = {}
        self._failed: Dict[str, str] = {}
        self._lxml = None
        try:
            from lxml import etree  # noqa: F401
            self._lxml = etree
        except Exception as exc:
            self._lxml = None
            self._import_error = str(exc)

    # ---------------------------------------------------------- 状态
    def is_available(self) -> bool:
        return self._lxml is not None and os.path.isdir(self.xsd_dir)

    def unavailable_reason(self) -> str:
        if self._lxml is None:
            return "未安装 lxml（pip install lxml）"
        return "找不到 XSD 目录 %s，先跑 tools/extract_xsd.py" % self.xsd_dir

    def known_schemas(self) -> List[str]:
        if not os.path.isdir(self.xsd_dir):
            return []
        return sorted(f for f in os.listdir(self.xsd_dir) if f.endswith(".xsd"))

    # ---------------------------------------------------------- 编译
    def _schema(self, name: str):
        if name in self._schemas:
            return self._schemas[name]
        if name in self._failed:
            return None
        path = os.path.join(self.xsd_dir, name)
        if not os.path.isfile(path):
            self._failed[name] = "缺少 %s" % name
            return None
        try:
            doc = self._lxml.parse(path)
            sch = self._lxml.XMLSchema(doc)
            self._schemas[name] = sch
            return sch
        except Exception as exc:
            self._failed[name] = str(exc).splitlines()[0][:200]
            return None

    # ---------------------------------------------------------- 校验
    def validate(self, schema_name: str, data: bytes) -> Tuple[str, List[str]]:
        """返回 (状态, 错误列表)。状态 ∈ ok / fail / unavailable。"""
        if not self.is_available():
            return "unavailable", [self.unavailable_reason()]
        sch = self._schema(schema_name)
        if sch is None:
            return "unavailable", [self._failed.get(schema_name, "schema 未就绪")]
        try:
            doc = self._lxml.fromstring(data)
        except Exception as exc:
            return "fail", ["XML 不合法: %s" % str(exc).splitlines()[0][:200]]
        if sch.validate(doc):
            return "ok", []
        errs = []
        for e in list(sch.error_log)[:12]:
            errs.append("line %s: %s" % (e.line, e.message.strip()[:200]))
        return "fail", errs

    def validate_package(self, pkg) -> Dict[str, Tuple[str, List[str]]]:
        """逐条目校验，键是包内路径。不认识的条目跳过。"""
        report: Dict[str, Tuple[str, List[str]]] = {}
        for name in sorted(pkg.entries):
            sch = schema_for_entry(name)
            if sch is None:
                continue
            report[name] = self.validate(sch, pkg.entries[name])
        return report

    def summary(self, report: Dict[str, Tuple[str, List[str]]]) -> Tuple[int, int, int]:
        """返回 (通过, 失败, 跳过)。"""
        ok = sum(1 for st, _ in report.values() if st == "ok")
        bad = sum(1 for st, _ in report.values() if st == "fail")
        skip = sum(1 for st, _ in report.values() if st == "unavailable")
        return ok, bad, skip

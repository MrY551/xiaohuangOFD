# -*- coding: utf-8 -*-
"""从标准 PDF 提取的全文里抽出附录 A 的 XSD，落成可直接用的 .xsd 文件。

    python tools/extract_xsd.py [--txt docs/GBT_33190-2016.txt] [--out docs/xsd]

为什么需要这一步：PDF 提取的文本有大量排版噪音——
  * 元素名和属性名粘在一起：`<xs:elementname="PhysicalBox"` 
  * 引号后直接跟下一个属性：`type="ST_Box"minOccurs="0"`
  * 小数点被拆行：`version="1. 0"`
  * 页眉页码插在中间：`GB/T33190—2016`、孤立的 `5`/`2`
这些都是排版的产物，不是标准内容，修复规则见 fix_layout()。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# XSD 里合法的 xs: 元素名
XS_ELEMENTS = (
    "schema|element|complexType|simpleType|sequence|choice|all|attribute|"
    "restriction|extension|enumeration|pattern|union|list|group|"
    "attributeGroup|import|include|annotation|documentation|appinfo|"
    "minInclusive|maxInclusive|minExclusive|maxExclusive|length|"
    "minLength|maxLength|whiteSpace|totalDigits|fractionDigits"
)

# XSD 里合法的属性名
XS_ATTRS = (
    "xmlns(?::[A-Za-z]+)?|name|type|use|ref|value|base|default|fixed|"
    "minOccurs|maxOccurs|maxLength|minLength|pattern|namespace|"
    "schemaLocation|noNamespaceSchemaLocation|form|id|"
    "substitutionGroup|nillable|abstract|processContents|mixed|"
    "targetNamespace|elementFormDefault|attributeFormDefault|version|"
    "encoding|itemType|memberTypes|standalone"
)


def fix_layout(text: str) -> str:
    """把 PDF 排版噪音还原成合法 XML 字面量。"""
    s = text
    # 页眉页码
    s = re.sub(r"=====\s*PAGE\s+\d+\s*=====", "\n", s)
    s = re.sub(r"^\s*GB/T\s*\d+[—\-]\d+\s*$", "", s, flags=re.M)
    # 孤立页码：整行只有 1~4 位数字
    s = re.sub(r"^\s*\d{1,4}\s*$", "", s, flags=re.M)

    # 引号内的换行先合并：PDF 会把长串拆行
    #   "1.\n0"            -> "1.0"
    #   "http://www.\nofdspec.\norg/2016" -> 完整 URL
    s = re.sub(r'"([^"]*)"',
               lambda m: '"' + re.sub(r"\s*\n\s*", "", m.group(1)) + '"',
               s, flags=re.S)

    # <? xmlversion="1.0"encoding="UTF-8"?>
    s = re.sub(r"<\?\s*xml\s*version", "<?xml version", s)
    s = re.sub(r'"encoding=', '" encoding=', s)
    s = re.sub(r'"\s*encoding="', '" encoding="', s)

    # 元素名与属性名粘连：<xs:elementname=  ->  <xs:element name=
    s = re.sub(r"(<xs:(?:%s))(%s)=" % (XS_ELEMENTS, XS_ATTRS), r"\1 \2=", s)
    # 上一个属性结束后直接接下一个属性：type="ST_Box"minOccurs="0"
    s = re.sub(r'"(?=(%s)=)' % XS_ATTRS, '" ', s)
    # 属性名与 = 之间被拆开：name = "x"
    s = re.sub(r"\b(%s)\s+=" % XS_ATTRS, r"\1=", s)
    # 注释符被拆：<! --   --> 
    s = s.replace("<! --", "<!--").replace("-- >", "-->")
    # 数字里的空格：minOccurs="1 0" 之类
    s = re.sub(r'="(\d+)\s+(\d+)"', r'="\1\2"', s)
    # 空行收敛
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s


# 标准正文的标题名 与 XSD 内部 include 引用的文件名对不上时，以引用方为准。
# 目前只有一处：A.6 标题写 "Definition. xsd"，但 include 写 "Definitions.xsd"。
NAME_FIX = {"Definition.xsd": "Definitions.xsd"}


def split_schemas(text: str):
    """按 `A. N Xxx.xsd` 切块，返回 [(文件名, 内容)]。"""
    # 标题形如：A. 3 Annotations. xsd
    marks = list(re.finditer(r"A\.\s*(\d+)\s+([A-Za-z][\w\.]*)\.\s*xsd", text))
    out = []
    for i, m in enumerate(marks):
        name = m.group(2).strip().rstrip(".")
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[start:end]
        # 从第一个 <?xml 开始，到最后一个 </xs:schema> 结束
        i0 = body.find("<?xml")
        if i0 < 0:
            i0 = body.find("<xs:schema")
        i1 = body.rfind("</xs:schema>")
        if i0 < 0 or i1 < 0:
            continue
        body = body[i0:i1 + len("</xs:schema>")]
        fname = name + ".xsd"
        fname = NAME_FIX.get(fname, fname)
        out.append((fname, fix_layout(body)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--txt", default=os.path.join(ROOT, "docs", "GBT_33190-2016.txt"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "xsd"))
    args = ap.parse_args(argv)

    if not os.path.isfile(args.txt):
        print("找不到全文:", args.txt)
        return 2
    text = open(args.txt, encoding="utf-8").read()

    # 附录 A 起点：第一次出现 "规范性附录" 之后的 schema
    a = text.find("规范性附录")
    if a < 0:
        a = text.find("附录A")
    body = text[a:] if a >= 0 else text

    items = split_schemas(body)
    if not items:
        print("没切出 XSD 块（附录 A 结构可能变了）")
        return 1

    os.makedirs(args.out, exist_ok=True)
    for name, content in items:
        path = os.path.join(args.out, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print("写出 %-24s %6d 字节" % (name, len(content.encode("utf-8"))))

    # 用 lxml 试编译，能编译的才算可用
    try:
        from lxml import etree
    except Exception as exc:
        print("\n没装 lxml，跳过编译校验:", exc)
        return 0

    print()
    order = ["Definitions.xsd", "OFD.xsd", "Document.xsd", "Res.xsd",
             "Annotations.xsd", "Annotation.xsd"]
    names = [n for n in order if os.path.isfile(os.path.join(args.out, n))]
    names += [n for n in os.listdir(args.out) if n not in names]
    ok = fail = 0
    for name in names:
        path = os.path.join(args.out, name)
        try:
            doc = etree.parse(path)
            etree.XMLSchema(doc)
            ok += 1
            print("  ok   %s" % name)
        except Exception as exc:
            fail += 1
            msg = str(exc).splitlines()[0][:150]
            print("  FAIL %s: %s" % (name, msg))
    print("\n编译通过 %d / %d" % (ok, ok + fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

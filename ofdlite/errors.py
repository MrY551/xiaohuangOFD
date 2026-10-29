# -*- coding: utf-8 -*-
"""ofdlite 的异常类型。全部继承 OfdError，调用方可以一个 except 兜住。"""


class OfdError(Exception):
    """OFD 处理相关错误基类。"""


class PackageError(OfdError):
    """zip 容器层面出错：不是 zip、缺 OFD.xml、条目损坏。"""


class ParseError(OfdError):
    """XML 结构不符合 GB/T 33190-2016 的最小要求。"""


class ResourceMissing(OfdError):
    """引用的资源（字体/图片/颜色空间/页面）在包里找不到。"""


class RenderError(OfdError):
    """渲染后端出错。"""


class PrintError(OfdError):
    """打印链路出错（无 CUPS、lp 返回非 0 等）。"""

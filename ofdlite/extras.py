# -*- coding: utf-8 -*-
"""文档级附属结构：标准里"不影响排版、但决定能不能用"的那几章。

   7.5 表8   文档权限声明 Permissions（打印/导出/编辑/份数/有效期）
   7.5 表9   视图首选项 VPreferences（窗口模式、页面布局、缩放模式）
   7.5 表10  书签 Bookmarks
   16  表63  自定义标引 CustomTags
   17  表64/65 扩展信息 Extensions
   18  表66-69 数字签名 Signatures
   19  表70/71 版本 Versions
   20  表72/73 附件 Attachments

这里只做**解析与呈现**：把文件里写了什么读成 dict，交给三端显示/判定。
不验签（要 SM2/SM3 与证书链）、不切换版本（要有版本文件树），
这两件事的边界在 docs/标准核对记录.md 附三十七里写清楚了。
"""
from __future__ import annotations

from typing import Dict, List, Optional

# xmlutil 由 model 在导入时注入，避免循环导入（model 也要用本模块）
X = None


def bind(xmlutil_module) -> None:
    global X
    X = xmlutil_module


# ------------------------------------------------------------------ 表8 权限

# 表8 的默认值：不写就是"都允许"
PERM_DEFAULTS = {
    "Edit": True, "Annot": True, "Export": True, "Signature": True,
    "Watermark": True, "PrintScreen": True,
    "Printable": True, "Copies": None,          # None = 份数不受限
    "StartDate": "", "EndDate": "",
}


def _flag(el, name, default=True):
    """取值写在属性上还是子元素正文里，两种都认。

    标准正文（表8/表9）把这些写成属性，附录 A 的 schema 写成子元素
    （`<Export>false</Export>`）。两套写法都合法，读的时候不能只认一种。
    """
    v = el.get(name)
    if v not in (None, ""):
        return X.bool_attr(v, default)
    ch = X.find(el, name)
    if ch is not None and (ch.text or "").strip():
        return X.bool_attr((ch.text or "").strip(), default)
    return default


def _text_of(el, name, default=""):
    v = el.get(name)
    if v not in (None, ""):
        return v.strip()
    ch = X.find(el, name)
    if ch is not None and (ch.text or "").strip():
        return (ch.text or "").strip()
    return default


def parse_permissions(root) -> dict:
    """表8：Permissions 是 Document 的直接子元素（在 Outlines 之后）。

    属性写法（正文）与子元素写法（附录 A）都认。
    """
    perm = dict(PERM_DEFAULTS)
    if root is None:
        return perm
    el = X.find(root, "Permissions")
    if el is None:
        return perm
    for key in ("Edit", "Annot", "Export", "Signature", "Watermark", "PrintScreen"):
        perm[key] = _flag(el, key, True)
    pr = X.find(el, "Print")
    if pr is not None:
        perm["Printable"] = X.bool_attr(pr.get("Printable"), True)
        if pr.get("Printable") in (None, ""):
            perm["Printable"] = _flag(pr, "Printable", True)
        copies = pr.get("Copies")
        if copies in (None, ""):
            ch = X.find(pr, "Copies")
            copies = (ch.text or "").strip() if ch is not None else None
        if copies not in (None, ""):
            try:
                perm["Copies"] = int(float(copies))
            except (TypeError, ValueError):
                perm["Copies"] = None
    vp = X.find(el, "ValidPeriod")
    if vp is not None:
        perm["StartDate"] = _text_of(vp, "StartDate", "")
        perm["EndDate"] = _text_of(vp, "EndDate", "")
    return perm


def printable_count(perm: dict) -> Optional[int]:
    """可打印份数（表8 原文口径）。

    Printable=false → 0；
    Printable=true 且没写 Copies、或 Copies 为负 → 不受限（None）；
    Copies=0 → 不允许打印；Copies>0 → 实际可打印份数。
    """
    if not perm or perm.get("Printable", True) is False:
        return 0
    copies = perm.get("Copies")
    if copies is None:
        return None
    if copies < 0:
        return None
    return copies


def validity_status(perm: dict, now=None) -> str:
    """表8 ValidPeriod：返回 ok / not-started / expired / unknown。

    原文："当不设置开始日期时代表不限定开始日期，不设置结束日期代表不限定
    结束日期"，"此文档允许访问的期限"。所以过期要能看见，不能当下不存在。
    """
    if not perm:
        return "ok"
    start = (perm.get("StartDate") or "").strip()
    end = (perm.get("EndDate") or "").strip()
    if not start and not end:
        return "ok"
    import datetime as _dt

    def _parse(text):
        t = text.strip().replace("Z", "+00:00")
        try:
            return _dt.datetime.fromisoformat(t)
        except ValueError:
            return None

    try:
        cur = now or _dt.datetime.now()
        s = _parse(start) if start else None
        e = _parse(end) if end else None
        if cur.tzinfo is None:
            s = s.replace(tzinfo=None) if (s and s.tzinfo) else s
            e = e.replace(tzinfo=None) if (e and e.tzinfo) else e
        if s and cur < s:
            return "not-started"
        if e and cur > e:
            return "expired"
    except Exception:
        return "unknown"
    return "ok"


# ------------------------------------------------------------------ 表9 视图

V_PREF_DEFAULTS = {
    "PageMode": "None", "PageLayout": "OneColumn", "TabDisplay": "FileName",
    "HideToolbar": False, "HideMenubar": False, "HideWindowUI": False,
    "ZoomMode": "Default", "Zoom": None,
}


def parse_vpreferences(root) -> dict:
    """表9：视图首选项。属性写法与子元素写法都认。"""
    pref = dict(V_PREF_DEFAULTS)
    if root is None:
        return pref
    el = X.find(root, "VPreferences")
    if el is None:
        return pref
    for key in ("PageMode", "PageLayout", "TabDisplay", "ZoomMode"):
        pref[key] = _text_of(el, key, pref[key]) or pref[key]
    for key in ("HideToolbar", "HideMenubar", "HideWindowUI"):
        pref[key] = _flag(el, key, False)
    zoom = _text_of(el, "Zoom", "")
    if zoom:
        pref["Zoom"] = _zoom_value(zoom)
    return pref


# ------------------------------------------------------------------ 表10 书签

def parse_bookmarks(root) -> List[dict]:
    """表10：Bookmarks/Bookmark[@Name]，Dest 见 CT_Dest（表54）。"""
    out: List[dict] = []
    if root is None:
        return out
    holder = X.find(root, "Bookmarks")
    if holder is None:
        return out
    for bm in X.find_all(holder, "Bookmark"):
        dest = X.find(bm, "Dest")
        out.append({
            "name": bm.get("Name") or "",
            "dest": parse_dest(dest),
        })
    return out


def _zoom_value(raw):
    """表9/表54 的 Zoom 是"缩放率"，标准没写单位是百分数还是小数。

    实测文件里有写 120 的（百分数）。这里两套都收：≤5 视为小数（1.2 = 120%），
    否则按百分数。两种情况都换算成**百分数**返回，前端统一除以 100。
    """
    if raw in (None, "", "0"):
        return None
    v = X.fnum(raw, None)
    if v is None or v <= 0:
        return None
    return v * 100.0 if v <= 5.0 else v


def parse_dest(el) -> Optional[dict]:
    """表54 目标区域。Left/Top 默认 0；Zoom 为 0 或不出现＝当前缩放。"""
    if el is None:
        return None
    return {
        "type": (el.get("Type") or "XYZ").strip(),
        "page_id": (el.get("PageID") or "").strip(),
        "left": X.fnum(el.get("Left"), 0.0),
        "top": X.fnum(el.get("Top"), 0.0),
        "right": None if el.get("Right") in (None, "") else X.fnum(el.get("Right"), 0.0),
        "bottom": None if el.get("Bottom") in (None, "") else X.fnum(el.get("Bottom"), 0.0),
        "zoom": _zoom_value(el.get("Zoom")),
    }


# ------------------------------------------------------------------ 表63 标引

def parse_custom_tags(pkg, entry: str) -> List[dict]:
    root = pkg.parse_xml(entry) if entry else None
    if root is None:
        return []
    out = []
    for el in X.find_all(root, "CustomTag"):
        schema_loc = X.find(el, "SchemaLoc")
        file_loc = X.find(el, "FileLoc")
        out.append({
            "namespace": el.get("NameSpace") or el.get("TypeID") or "",
            "schema": (schema_loc.text or "").strip() if schema_loc is not None else "",
            "file": (file_loc.text or "").strip() if file_loc is not None else "",
            "entry": pkg.resolve(entry, (file_loc.text or "").strip())
            if file_loc is not None else "",
        })
    return out


# ------------------------------------------------------------------ 表64/65 扩展

def parse_extensions(pkg, entry: str) -> List[dict]:
    root = pkg.parse_xml(entry) if entry else None
    if root is None:
        return []
    out = []
    for el in X.find_all(root, "Extension"):
        props = []
        for pr in X.find_all(el, "Property"):
            props.append({"name": pr.get("Name") or "",
                          "type": pr.get("Type") or "",
                          "value": (pr.text or "").strip()})
        data = X.find(el, "Data")
        extend = X.find(el, "ExtendData")
        out.append({
            "app": el.get("AppName") or "",
            "company": el.get("Company") or "",
            "app_version": el.get("AppVersion") or "",
            "date": el.get("Date") or "",
            "ref_id": el.get("RefId") or "",
            "properties": props,
            "has_data": data is not None,
            "extend_data": (extend.text or "").strip() if extend is not None else "",
        })
    return out


# ------------------------------------------------------------------ 表66-69 签名

def parse_signatures(pkg, entry: str) -> dict:
    """签名列表（表66）；逐个签名再读它的描述文件（表67-69）。

    只解析、不验签：验签要按 CheckMethod 重算摘要并用 SM2/SM3 验证签名值，
    那是密码模块的活，本工具不做，原文条目一律按字节保留。
    """
    info = {"max_sign_id": "", "items": [], "entry": entry}
    root = pkg.parse_xml(entry) if entry else None
    if root is None:
        return info
    # 表66 说 MaxSignId 是属性；附录 A 的 Signatures.xsd 把它写成子元素。两种都认。
    ms = root.get("MaxSignId")
    if ms in (None, ""):
        ch = X.find(root, "MaxSignId")
        ms = (ch.text or "").strip() if ch is not None else ""
    info["max_sign_id"] = ms or ""
    for el in X.find_all(root, "Signature"):
        base = (el.get("BaseLoc") or "").strip()
        desc_entry = pkg.resolve(entry, base) if base else ""
        item = {
            "id": el.get("ID") or "",
            # 表66：Type 可选，附录 A 的 default 是 Seal
            "type": (el.get("Type") or "Seal").strip(),
            "base_loc": base,
            "entry": desc_entry,
            "provider": "", "datetime": "", "method": "",
            "references": [], "stamp_annots": [], "signed_value": "", "seal": "",
        }
        sroot = pkg.parse_xml(desc_entry) if desc_entry else None
        if sroot is not None:
            si = X.find(sroot, "SignedInfo")
            src = si if si is not None else sroot
            prov = X.find(src, "Provider")
            if prov is not None:
                item["provider"] = (prov.get("ProviderName")
                                    or (prov.text or "")).strip()
            # 附录 A：SignatureDateTime / SignatureMethod 是元素；正文表格把它们
            # 列成"属性"。两种都认，别只读属性。
            dt = X.find(src, "SignatureDateTime")
            item["datetime"] = ((dt.text or "").strip() if dt is not None
                                else (src.get("SignatureDateTime") or ""))
            sm = X.find(src, "SignatureMethod")
            item["method"] = ((sm.text or "").strip() if sm is not None
                              else (src.get("SignatureMethod") or ""))
            refs = X.find(src, "References")
            if refs is not None:
                # 表68：CheckMethod 可选，附录 A 的 default 是 MD5
                method = refs.get("CheckMethod") or "MD5"
                for rf in X.find_all(refs, "Reference"):
                    cv = X.find(rf, "CheckValue")
                    item["references"].append({
                        "file": (rf.get("FileRef") or "").strip(),
                        "method": method,
                        "value": ((cv.text or "").strip() if cv is not None
                                  else (rf.get("CheckValue") or "")),
                    })
            for sa in X.find_all(src, "StampAnnot"):
                item["stamp_annots"].append({
                    "page_ref": (sa.get("PageRef") or "").strip(),
                    "id": (sa.get("ID") or "").strip(),
                    "boundary": (sa.get("Boundary") or "").strip(),
                    "clip": (sa.get("Clip") or "").strip(),
                })
            seal = X.find(src, "Seal")
            if seal is not None:
                loc = X.find(seal, "BaseLoc")
                item["seal"] = ((loc.text or "").strip() if loc is not None
                                else (seal.get("BaseLoc") or (seal.text or "")).strip())
            # 附录 A：SignedValue 是 ST_Loc 元素（文本），正文写的是 BaseLoc 属性
            sv = X.find(sroot, "SignedValue")
            if sv is not None:
                item["signed_value"] = ((sv.text or "").strip()
                                        or (sv.get("BaseLoc") or "")).strip()
        info["items"].append(item)
    return info


def describe_signatures(info: dict) -> str:
    if not info or not info.get("items"):
        return "无签名"
    parts = []
    for it in info["items"]:
        kind = {"seal": "签章", "sign": "签名"}.get(it["type"].lower(), it["type"] or "签名")
        parts.append("%s#%s %s" % (kind, it["id"], it["datetime"] or "（无时间）"))
    return "、".join(parts)


# ------------------------------------------------------------------ 表70/71 版本

def parse_versions(pkg, entry: str) -> dict:
    """版本列表（表70）：既能是独立文件，也能内嵌在 OFD.xml（附录 A 就是这么定义的）。"""
    info = {"entry": entry, "items": [], "current": None}
    root = pkg.parse_xml(entry) if entry else None
    if root is None:
        return info
    return parse_versions_el(root, pkg, entry)


def parse_versions_el(root, pkg, base_entry: str) -> dict:
    """从一个已解析的 Versions 元素取版本条目（BaseLoc 相对 base_entry）。"""
    info = {"entry": base_entry, "items": [], "current": None}
    if root is None:
        return info
    for el in X.find_all(root, "Version"):
        base = (el.get("BaseLoc") or "").strip()
        desc = pkg.resolve(base_entry, base) if base else ""
        item = {
            "id": el.get("ID") or "",
            "index": el.get("Index") or "",
            "current": X.bool_attr(el.get("Current"), False),
            "base_loc": base,
            "entry": desc,
            "name": "", "format_version": "", "creation": "",
            "files": [], "doc_root": "",
        }
        vroot = pkg.parse_xml(desc) if desc else None
        if vroot is not None:
            item["name"] = vroot.get("Name") or ""
            item["format_version"] = vroot.get("Version") or ""
            item["creation"] = vroot.get("CreationDate") or ""
            fl = X.find(vroot, "FileList")
            if fl is not None:
                for f in X.find_all(fl, "File"):
                    item["files"].append({
                        "id": f.get("ID") or "",
                        "path": (f.text or "").strip(),
                    })
            dr = X.find(vroot, "DocRoot")
            if dr is not None:
                item["doc_root"] = (dr.text or "").strip()
        info["items"].append(item)
    for it in info["items"]:
        if it["current"]:
            info["current"] = it["id"]
            break
    return info


# ------------------------------------------------------------------ 表72/73 附件

def parse_attachments(pkg, entry: str) -> List[dict]:
    root = pkg.parse_xml(entry) if entry else None
    if root is None:
        return []
    out = []
    for el in X.find_all(root, "Attachment"):
        loc = X.find(el, "FileLoc")
        href = (loc.text or "").strip() if loc is not None else ""
        size = el.get("Size")
        out.append({
            "id": el.get("ID") or "",
            "name": el.get("Name") or "",
            "format": el.get("Format") or "",
            "creation": el.get("CreationDate") or "",
            "modified": el.get("ModDate") or "",
            "size_kb": None if size in (None, "") else X.fnum(size, 0.0),
            "visible": X.bool_attr(el.get("Visible"), True),
            "usage": el.get("Usage") or "none",
            "file_loc": href,
            "entry": pkg.resolve(entry, href) if href else "",
        })
    return out


def attachment_entry(att: dict) -> str:
    """附件内容所在条目（表73 的 FileLoc 指向附件内容文件本身）。"""
    return att.get("entry") or ""

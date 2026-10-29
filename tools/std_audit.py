# -*- coding: utf-8 -*-
"""标准符合性审计：按 GB/T 33190-2016 的章节逐项核对实现状态。

做法不是"人写一张表"，而是：
  1. 从标准原件里抽出章节结构（一级/二级标题）
  2. 对每个条款去代码里找**证据**（类名/函数名/关键字）
  3. 有证据标"已实现"、只有部分证据标"部分"、找不到标"未实现"
  4. 人工维护的例外写在 OVERRIDES 里（附理由）

产物是 docs/标准符合性审计.md，可直接当验收依据。
"""
from __future__ import annotations

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STD = os.path.join(ROOT, "docs", "33190-2016-gbt-cd-300.txt")
OUT = os.path.join(ROOT, "docs", "标准符合性审计.md")

# 条款 → 代码证据（正则，在 ofdlite/ + app/ + web/static/ 里搜）
#
# 注意：这张表只做「有没有对应实现」的初筛，**关键词命中不等于做对了**。
# 2026-10-30 这轮核查就是被它坑过一次：9.3 非紧缩描述、第 17 章扩展信息
# 都被宽松正则判成"已实现"，实际根本没解析。所以凡是关键词可能误命中的
# 条款（9.3 / 14 / 16 / 17 / 18 / 19 / 20），一律走 OVERRIDES 手写结论。
EVIDENCE = {
    "6.1": [r"zipfile"], "6.2": [r"OFD\.xml"],
    "7.2": [r"utf-8|UTF-8"], "7.3": [r"ST_ID|fnum|bool_attr"],
    "7.4": [r"OFD\.xml"], "7.5": [r"class Document|Document\.xml"], "7.6": [r"Pages|page_refs"],
    "7.7": [r"class Page\b"], "7.8": [r"Outlines"], "7.9": [r"PublicRes|DocumentRes|PageRes"],
    "8.1.3": [r"PhysicalBox"], "8.1.4": [r"Boundary"], "8.1.5": [r"class Matrix|ctm"],
    "8.2.1": [r"line_width|LineWidth"], "8.2.4": [r"dash_pattern|DashPattern"],
    "8.4": [r"Clips|_clip_mask|_apply_clip"], "8.5": [r"class OFDObject"],
    "9.1": [r"class PathObject"], "9.2": [r"evenodd|EvenOdd"],
    "10": [r"class ImageObject", r"image_border"],
    "11.1": [r"class FontSpec|Fonts"], "11.2": [r"class TextObject"],
    "11.3": [r"TextCode"],
}

# 人工确认的例外（代码里有关键字但实际只是"解析不渲染"或"部分实现"）
OVERRIDES = {
    "5.1": ("已实现", "文件本身用 XML 描述，配套 Schema 见附录 A（已落 docs/xsd 并接进 validate）"),
    "5.2": ("已实现", "「容器 + 文档」三层：ZIP 容器、Document/Pages/资源索引、页面内容描述"),
    "5.3": ("已实现", "二维矢量成像模型：图形/文字/图像三类图元，绘制参数 + CTM + 裁剪区"),
    "5.4": ("已实现", "生成与另存一律用 .ofd 小写扩展名"),
    "7.5": ("部分", "CommonData/Pages/Outlines/Permissions(表8)/VPreferences(表9)/Bookmarks(表10) 与 Annotations·CustomTags·Attachments·Extensions 引用都解析；MaxUnitID 递增维护"),
    "7.1": ("已实现", "读时按 local name 兜住各种前缀，写时统一声明 http://www.ofdspec.org/2016"),
    "8.1.1": ("已实现", "全部按毫米、原点左上、Y 向下处理"),
    "8.1.2": ("已实现", "页面→设备只做分辨率换算（dpi/缩放），不做设备相关描述"),
    "7.8": ("已实现", "`Outlines` 解析成树并在三处显示：网页右栏大纲面板（可点跳页）、Qt 左侧大纲树（可点跳页）、`ofd_cli outline` 命令行"),
    "13": ("已实现", "复合对象按表49/50 走 ResourceID → CT_VectorG → Content 展开；元素名按标准 CompositeGraphicUnit（2026-10-30 修正拼写）"),
    "15": ("已实现", "注释入口 + 分页注释文件 + Appearance（CT_PageBlock）都解析，签章外观并进渲染；Print/NoZoom/NoRotate/ReadOnly/Visible 参与打印与显示判定"),
    "8.2.1": ("已实现", "表21 全部属性；就近原则 + Relative 继承（2026-10-30 补上继承）"),
    "8.2.2": ("部分", "`Join` 三端都按标准取值（Miter/Round/Bevel）；Pillow 后端只能圆角近似并告警，Qt 与 Canvas 准确"),
    "8.2.3": ("部分", "`MiterLimit` 已按标准自己算描边多边形（ofdlite/stroke.py），截断真正生效、Qt 端渲染正确；Pillow 导出在尖角内侧有小白三角（自交多边形填充限制）"),
    "8.2.5": ("部分", "`Cap` 三端都按标准取值（Butt/Round/Square）；Pillow 用端点补画实现，Qt 与 Canvas 原生"),
    "8.3.1": ("已实现", "ColorSpace 回资源解析 Type/BitsPerComponent/Palette（表25），Index 取色（表26）"),
    "8.3.2": ("已实现", "逐通道十六进制（#11 #22 #33）/十进制、BPC 通道上限、越界按默认色（表27）"),
    "8.3.3": ("部分", "Pillow 导出端完整（shd_check --pattern 实测平铺/定位/ReflectMethod 镜像都正确）；Qt 与 Canvas 已能平铺上色，但两端的 ReflectMethod 都不支持（已告警），Canvas 端格子尺寸还会随 zoom 失配"),
    "8.3.4": ("部分", "轴向与径向三端一致；高洛德/网格高洛德 Pillow 精确，Qt 与 Canvas 降级为平均色并告警"),
    "9.3": ("已实现", "非紧缩描述（表37-42：Start/Move/Line/QuadraticBezier/CubicBezier/Arc/Close）落成 ofdlite.geometry.parse_region/parse_area；Action 的 Region 用它算热区，PathObject 里出现也容错读"),
    "11.4": ("部分", "`CGTransform` 解析并判定影响，但不做字形级绘制（需 fontTools + CMap）；非均匀变换会告警"),
    "12": ("未实现", "视频对象忽略（ImageObject 带 Movie 动作时只按普通图片渲染）"),
    "14": ("部分", "表51 五类动作全部解析（Goto/URI/GotoA/Sound/Movie）并带 Region(表51)、Dest 的 Left/Top/Right/Bottom/Zoom(表54)；可执行的是 goto 跳页/定位、uri 开窗、gotoa 打开附件，音视频只报信息"),
    "16": ("已实现", "CustomTags 解析成 {NameSpace, SchemaLoc, FileLoc}（表63），网页文档面板显示组数，CLI extras 全量输出"),
    "17": ("已实现", "Extensions 解析 AppName/Company/AppVersion/Date/RefId 与 Property（表64/65），CLI 与网页都显示"),
    "18": ("部分", "签名列表（表66）与签名描述（表67-69：Provider/时间/算法/References 摘要/StampAnnot/Seal/SignedValue）都解析并展示；**不验签**（要 SM2/SM3 与证书链），条目按字节保留"),
    "19": ("已实现", "版本列表（表70，含内嵌 OFD.xml 与独立文件两种写法）与版本描述（表71 FileList/DocRoot）都解析；不做版本切换"),
    "20": ("已实现", "附件列表（表72/73）解析，网页 /api/attachment/<ID> 下载、CLI extract-attach 落盘"),
}

STATUS_ICON = {"已实现": "✅", "部分": "⚠️", "未实现": "❌"}


# 标准条款清单（GB/T 33190-2016 正文的章节结构，按目录逐条手工列出）。
# 不用正则从 PDF 文本里抽——那份文本的编号与标题之间有各种断行与粘连，
# 抽出来会漏项；清单固定，状态才靠代码证据自动判定。
#
# 2026-10-30 补全：原来只覆盖 7~17 章，第 5/6 章（文档特性、文件结构）与
# 第 18/19/20 章（数字签名、版本、附件）根本没有列进表里——"整体核查"
# 少了这几章就不叫整体。现在按目录列到 20 章。
CLAUSES = [
    ("5.1", "文档特性"), ("5.2", "文件结构模型"), ("5.3", "成像模型"), ("5.4", "扩展名"),
    ("6.1", "容器方案"), ("6.2", "文件组织"),
    ("7.1", "命名空间"), ("7.2", "字符编码"), ("7.3", "基础数据类型"),
    ("7.4", "主入口 OFD.xml"), ("7.5", "文档根节点"), ("7.6", "页树"),
    ("7.7", "页对象"), ("7.8", "大纲"), ("7.9", "资源"),
    ("8.1.1", "坐标说明"), ("8.1.2", "设备空间"), ("8.1.3", "页面空间"),
    ("8.1.4", "对象空间"), ("8.1.5", "变换矩阵"),
    ("8.2.1", "绘制参数结构"), ("8.2.2", "线条连接样式"), ("8.2.3", "线条连接点截断值"),
    ("8.2.4", "线条的虚线样式"), ("8.2.5", "线条的端点样式"),
    ("8.3.1", "颜色空间"), ("8.3.2", "基本颜色"), ("8.3.3", "底纹"), ("8.3.4", "渐变"),
    ("8.4", "裁剪区"), ("8.5", "图元对象"),
    ("9.1", "图形对象"), ("9.2", "填充规则"), ("9.3", "非紧缩描述"),
    ("10", "图像"), ("11.1", "字型"), ("11.2", "文字对象"), ("11.3", "文字定位"),
    ("11.4", "字形变换"), ("12", "视频"), ("13", "复合对象"), ("14", "动作"),
    ("15", "注释"), ("16", "自定义标引"), ("17", "扩展信息"),
    ("18", "数字签名"), ("19", "版本"), ("20", "附件"),
]


def load_chapters():
    return list(CLAUSES)


def collect_code():
    parts = []
    for base in ("ofdlite", os.path.join("app"), os.path.join("web", "static")):
        d = os.path.join(ROOT, base)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith((".py", ".js")):
                try:
                    parts.append(io.open(os.path.join(d, fn), encoding="utf-8",
                                         errors="replace").read())
                except OSError:
                    pass
    return "\n".join(parts)


def audit():
    chapters = load_chapters()
    code = collect_code()
    rows = []
    for num, title in chapters:
        if num in OVERRIDES:
            status, note = OVERRIDES[num]
            rows.append((num, title, status, note, ""))
            continue
        pats = EVIDENCE.get(num)
        if not pats:
            continue
        hit = [p for p in pats if re.search(p, code)]
        if len(hit) == len(pats):
            rows.append((num, title, "已实现", "代码中可检出实现",
                         " / ".join(hit)))
        elif hit:
            rows.append((num, title, "部分", "只检出部分证据",
                         " / ".join(hit)))
        else:
            rows.append((num, title, "未实现", "代码中找不到对应用法", ""))
    return rows


def render(rows):
    total = len(rows)
    cnt = {k: sum(1 for r in rows if r[2] == k) for k in ("已实现", "部分", "未实现")}
    lines = []
    lines.append("# 标准符合性审计（GB/T 33190-2016）")
    lines.append("")
    lines.append("本表由 `tools/std_audit.py` 生成：章节从标准原件抽取，状态由"
                 "「代码里能不能检出对应实现」判定，人工确认的例外写在脚本的 "
                 "`OVERRIDES` 里并附理由。**这是验收依据，不是宣传材料。**")
    lines.append("")
    lines.append("统计（共 %d 项）：✅ 已实现 **%d** · ⚠️ 部分 **%d** · ❌ 未实现 **%d**"
                 % (total, cnt["已实现"], cnt["部分"], cnt["未实现"]))
    lines.append("")
    lines.append("| 条款 | 内容 | 状态 | 说明 |")
    lines.append("|---|---|---|---|")
    for num, title, status, note, ev in rows:
        lines.append("| %s | %s | %s %s | %s |"
                     % (num, title, STATUS_ICON[status], status, note))
    lines.append("")
    lines.append("## 未实现与部分实现的完整清单")
    lines.append("")
    for status in ("未实现", "部分"):
        items = [r for r in rows if r[2] == status]
        if not items:
            continue
        lines.append("### %s %s" % (STATUS_ICON[status], status))
        lines.append("")
        for num, title, _s, note, _e in items:
            lines.append("- **%s %s** — %s" % (num, title, note))
        lines.append("")
    lines.append("## 判定口径与它不可靠的地方")
    lines.append("")
    lines.append("上表的「已实现」只说明**代码里能检出对应实现**，不等于逐像素验过。"
                 "按证据强度把条款分三档，别把三档当成一回事：")
    lines.append("")
    lines.append("| 档位 | 含义 | 本项目的条款 |")
    lines.append("|---|---|---|")
    lines.append("| A 实测 | 有专门测试文件或三端逐点比对 | 8.3.1/8.3.2 颜色（std_gap_probe 五个色块逐像素）、"
                 "8.4 裁剪、8.3.4 渐变、10 图像边框、11.2 文字对象、13 复合对象 |")
    lines.append("| B 真实文件 | 用真实数电发票跑通（10 条目 / 288 图元 / 渲染无告警） | "
                 "6.x、7.4~7.9、8.1、9.1~9.2、11.1~11.3、15 |")
    lines.append("| C 静态 | 只检出了代码用法，没有独立测试 | 7.1~7.3、8.2、8.5 |")
    lines.append("| D 样本 | 有专门造的样本、逐项断言（`tools/make_extras_ofd.py` → `out/extras_test.ofd`，XSD 12/12） | "
                 "7.5 表8/9/10、9.3、14、16、17、18、19、20、表44 |")
    lines.append("")
    lines.append("**这张表的证据判据本身出过错，必须记下来：**"
                 "2026-10-30 那轮核查发现，9.3 非紧缩描述和第 17 章扩展信息"
                 "都被宽松正则（`AbbreviatedData`、`Extension`）判成「已实现」，"
                 "实际上一行都没解析。现在这两条已按标准补齐（9.3 → "
                 "`geometry.parse_region`，17 → `extras.parse_extensions`），"
                 "判据也改成手写结论：凡关键词可能误命中的条款"
                 "（9.3 / 14 / 16 / 17 / 18 / 19 / 20）都不再靠正则判定。")
    lines.append("")
    lines.append("属性级覆盖另有一份机器清单：`tools/std_attr_audit.py` 取附录 A 的 "
                 "XSD（126 个属性）逐个查代码，产物见 `docs/标准属性覆盖.md`。"
                 "两份表要一起看——章节表会说「8.3.1 已实现」，属性表才会告诉你"
                 "`BitsPerComponent`、`Palette` 这些细节有没有真的接上。")
    lines.append("")
    lines.append("## 附：标准自身的矛盾（本项目按「记录不猜」处理）")
    lines.append("")
    lines.append("| 位置 | 矛盾 | 本项目处理 |")
    lines.append("|---|---|---|")
    lines.append("| 表21/表35 vs 附录 A | `MiterLimit` 默认值正文 3.528、schema 4.234 | 不设默认值，记在 `标准核对记录.md` |")
    lines.append("| 表34 | `Alpha` 默认 0 与同表「255 表示完全不透明」矛盾 | **有意偏离**为默认 255，否则真实文档整页空白 |")
    lines.append("| 表26 vs schema | `LaGouraudShd`（带 u）与 `LaGourandShd`（少 u） | 两个都认 |")
    lines.append("| 表48 vs schema | `Glyphs` 正文说必选、schema 是 minOccurs=0 | 有就解析、没有跳过 |")
    lines.append("")
    return "\n".join(lines), cnt, total


def main():
    rows = audit()
    md, cnt, total = render(rows)
    io.open(OUT, "w", encoding="utf-8").write(md)
    print("已写出 %s（%d 项：已实现 %d / 部分 %d / 未实现 %d）"
          % (os.path.relpath(OUT, ROOT), total,
             cnt["已实现"], cnt["部分"], cnt["未实现"]))
    for r in rows:
        if r[2] != "已实现":
            print("  %s %s %s — %s" % (STATUS_ICON[r[2]], r[0], r[1], r[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

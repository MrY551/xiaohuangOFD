# 第三方组件与许可

小黄OFD（`ofdlite`、`app`、`web`、`tools` 合称，包名 xiaohuang-ofd）按 `LICENSE` 发布，全文见 `LICENSE`。
下面是它依赖的第三方组件及其许可。分发（尤其是打进 deb / 单文件可执行包）时要连同这份声明一起带出去。

## 运行时依赖

| 组件 | 版本 | 许可 | 是否随包分发 | 说明 |
|---|---|---|---|---|
| Python 标准库 | 3.7+ | PSF License | 否（依赖目标机系统的 python3） | zipfile、xml.etree、http.server、argparse 等 |
| Pillow | 9+ | MIT-CMU（HPND） | **否**（deb 里是 `Depends: python3-pil`，由系统包提供） | 仅 Pillow 渲染后端用；不用它也能跑桌面端与网页端 |
| PySide6 / PySide2 / PyQt5 | 任一 | LGPL-3.0 / GPL-3.0 / 商业（PySide）；GPL-3.0 / 商业（PyQt） | **否**（deb 里是 `Recommends`） | 只有桌面端需要，三选一。见下方「Qt 的 LGPL 义务」 |
| cups-client（lp / lpstat） | 系统包 | GPL-2.0 / LGPL-2.1 | 否 | 打印功能靠命令行调用 `lp` |
| 系统字体 | — | 各字体自己的许可 | 否 | 不内嵌、不打包，运行时按名称从系统字体目录解析 |

`ofdlite` 核心库只依赖 Python 标准库；Pillow 只被 `render_pil.py` 用到，导出图片/PDF 时才 import。

## Qt 的 LGPL 义务

deb 里 Qt 是 `Recommends`，不随包分发，此时不触发 LGPL 的分发义务。

**一旦你用 PyInstaller / cx_Freeze / AppImage 把 Qt 库打进产物，就等于分发了 Qt**，必须做到：

1. 声明使用了 Qt / PySide6，附带其 LGPL-3.0 全文；
2. 允许最终用户替换 Qt 库（动态链接、不剥离符号、不阻止替换 DLL/so）；
3. 提供 Qt 源码的获取方式（或指向官方下载地址）。

做不到第 2 条（比如做成完全静态链接、或对库做了不可替换的封装），就得改用商业授权，或者不要打包 Qt——保持依赖系统包。

## 实现来源

本项目的 OFD 解析、渲染、编辑、打印全部逻辑，**依据 GB/T 33190-2016《电子文件存储与交换格式 版式文档》
独立实现**，不含任何第三方代码。

实现过程是逐条对着标准正文做的，不是照别人的实现改：

- 通读全文 132 页，按章节落地（文件结构、基本结构、页面描述、图形、图像、文字、注释、
  自定义标引、数字签名、版本、附件）；
- 逐表核对属性语义与默认值，下面这些表都对应到了具体代码；
- 把附录 A 的 schema 全部抽出来编译，当作结构校验器用（`tools/validate.py`）；
- 用真实电子发票反向验证，反过来查出文件本身的不合规点。

核对过的表格（条款号 ↔ 代码位置见 `docs/标准核对记录.md`）：

| 表 | 内容 | 落到哪里 |
|---|---|---|
| 表 2 | 基础数据类型（`ST_ID` 是无符号整数、`ST_Box`、`ST_Pos`） | `xmlutil` / `geometry` |
| 表 6 | 文档公共数据（`MaxUnitID`、`PageArea`、`TemplatePage`） | `model.Document` |
| 表 12 | 页对象（`Area` 回退链、`PageRes`、`Template`、`Content`） | `model.Page` |
| 表 13–15 | 模板页、图层、层类型（`Body`/`Foreground`/`Background`） | `model.Layer` / `scene` |
| 表 34–35 | 图元对象与图形对象（`Visible`、`Clips`、`Stroke` 默认 true、`Fill` 默认 false） | `model.OFDObject` / `PathObject` |
| 表 36 | 紧缩描述运算符（`S`/`M`/`L`/`Q`/`B`/`A`/`C`） | `geometry.parse_path_data` |
| 表 44–45 | 字型与文字对象（`FontFile` 可选、`HScale`、`Weight`、`Fill` 默认 true） | `model.TextObject` / `font_manager` |
| 表 46–47 | 文字定位与排列方向（`DeltaX` 语义、`CharDirection`/`ReadDirection`） | `scene._emit_text` |
| 表 60–62 | 注释（列表索引、`PageAnnot`/`Annot`、五种类型含 `Stamp`） | `model.Annotation` |
| 附录 A | 14 个 XSD（含 `Page.xsd` 里的 `CT_Path`/`CT_Text` 子元素顺序） | `docs/xsd/` / `ofdlite/schema.py` |

标准原文、抽出的 schema、以及每处实现对应的条款号都在 `docs/`：

- `docs/GBT_33190-2016.pdf` / `.txt` —— 标准全文与可检索的文本层
- `docs/xsd/` —— 附录 A 的 14 个 schema（全部通过 lxml 编译）
- `docs/标准核对记录.md` —— 逐条对照记录：确认正确的、改过的、还没覆盖的

## 数据与隐私

- 网页端只监听 `127.0.0.1`，不对外提供服务，没有账号体系，也不上传任何文档内容。
- 解析、渲染、导出全在本机完成，不联网取字体、不联网校验。
- 保存时默认在同目录留 `.bak` 备份（`OFDPackage.save(keep_backup=True)`）。

## 免责

本软件按「现状」提供，不附带任何明示或暗示的担保，包括但不限于适销性、特定用途适用性与不侵权担保。
作者与贡献者不对使用本软件产生的任何索赔、损害或其他责任负责，无论是在合同、侵权还是其他方面。

具体到 OFD 场景，请注意：

- 版式文档的渲染结果依赖本机字体，与原始生成器可能有一到两个百分点的度量差异；
- 本软件不校验数字签名、不验证签章有效性，也不能作为发票真伪的依据；
- 涉及票据、公文、合同的操作，请保留原件与备份，并以官方阅读器的显示结果为准。

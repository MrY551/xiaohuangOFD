#!/bin/bash
# 打包成 deb，供银河麒麟 V10 / aarch64（以及 amd64 桌面）安装。
#
#   ./packaging/build_deb.sh            # 产出 dist/xiaohuang-ofd_1.0.0_all.deb
#   ./packaging/build_deb.sh --deps     # 同时打印运行时依赖检查结果
#
# 纯 Python 包，架构无关（Architecture: all）。
# Qt 绑定与 Pillow 不进包，由系统包管理器或 pip 提供，避免和系统库打架。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="xiaohuang-ofd"
 # 版本取自 ofdlite.__version__（唯一源头）。注意 Debian 的排序规则里字母 < 数字，
 # 所以带 beta 后缀的版本会排在同名正式版之后；要发预发布版请用 1.1.0~beta 形式。
VERSION="$(python3 -c "import sys;sys.path.insert(0,'$ROOT');import ofdlite;print(ofdlite.__version__)" 2>/dev/null || echo 1.0.0)"
ARCH="all"
STAGE="${OFD_BUILD_DIR:-${TMPDIR:-/tmp}/ofd-deb-build}"
DIST="$ROOT/dist"
PKGROOT="$STAGE/${NAME}_${VERSION}_${ARCH}"

echo "== 打包 $NAME $VERSION ($ARCH)"
rm -rf "$STAGE"
mkdir -p "$PKGROOT/DEBIAN"
mkdir -p "$PKGROOT/usr/lib/xiaohuang-ofd"
mkdir -p "$PKGROOT/usr/bin"
mkdir -p "$PKGROOT/usr/share/applications"
mkdir -p "$PKGROOT/usr/share/icons/hicolor/256x256/apps"
mkdir -p "$PKGROOT/usr/share/doc/$NAME"
mkdir -p "$DIST"

# ---------------------------------------------------------------- 程序文件
cp -r "$ROOT/ofdlite" "$PKGROOT/usr/lib/xiaohuang-ofd/"
cp -r "$ROOT/app" "$PKGROOT/usr/lib/xiaohuang-ofd/"
cp -r "$ROOT/web" "$PKGROOT/usr/lib/xiaohuang-ofd/"
cp -r "$ROOT/tools" "$PKGROOT/usr/lib/xiaohuang-ofd/"
cp "$ROOT/README.md" "$PKGROOT/usr/share/doc/$NAME/" 2>/dev/null || true
cp "$ROOT/LICENSE" "$PKGROOT/usr/share/doc/$NAME/" 2>/dev/null || true
cp "$ROOT/THIRD-PARTY-NOTICES.md" "$PKGROOT/usr/share/doc/$NAME/" 2>/dev/null || true
cp "$ROOT/docs/法律核查.md" "$PKGROOT/usr/share/doc/$NAME/" 2>/dev/null || true
cp "$ROOT/docs/专利点声明.md" "$PKGROOT/usr/share/doc/$NAME/" 2>/dev/null || true
# drvfs/9p 上的源文件权限不可靠，cp 会带过来；文档必须是 0644
chmod 0644 "$PKGROOT/usr/share/doc/$NAME/"* 2>/dev/null || true

# Debian 要求 /usr/share/doc/<pkg>/copyright，指向许可与第三方声明
cat > "$PKGROOT/usr/share/doc/$NAME/copyright" <<'COPYRIGHT'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: xiaohuang-ofd
Upstream-Contact: 小黄 <1924053793@qq.com>
Source: 依据 GB/T 33190-2016 独立实现

Files: *
Copyright: 2026 小黄 <15359927763> QQ 1924053793
License: Xiaohuang-Software-Licence
 著作权归小黄所有（自创作完成产生）；专利申请权与商标使用权保留，
 截至本版本均未提交申请（详见 LICENSE 的权利声明）。
 可自由安装、使用、复制、修改、分发，用于个人或商业用途，可以收费销售。
 唯一硬条件：无论分发原版还是修改版（含只发二进制），必须保留本署名与
 联系方式，让最终用户能找到作者。
 允许改名，但需在版本信息/网页页脚/安装包名任一处注明「基于 小黄OFD 修改」
 并保留原项目地址。
 不允许删除或替换作者署名后以其他名义发布，也不允许把本软件包装成他人作品。
 本软件按「现状」提供，不附带任何担保；文档损坏、打印偏差、签名失效等后果
 由使用者自行承担。本软件不校验数字签名、不验证签章有效性。
 .
 完整许可原文见 /usr/share/doc/xiaohuang-ofd/LICENSE。
 第三方组件（Python/Pillow/cups-client/可选 Qt）的许可见
 /usr/share/doc/xiaohuang-ofd/THIRD-PARTY-NOTICES.md。
COPYRIGHT
chmod 0644 "$PKGROOT/usr/share/doc/$NAME/copyright"

# 构建指纹：官方发布记录里的值，可供任何人核对某份副本是否原版
BUILD_ID_VAL=$(python3 -c "import sys;sys.path.insert(0,'$ROOT');from ofdlite.buildid import build_id;print(build_id())" 2>/dev/null || echo unknown)
echo "$BUILD_ID_VAL" > "$PKGROOT/usr/share/doc/$NAME/BUILD_ID"
chmod 0644 "$PKGROOT/usr/share/doc/$NAME/BUILD_ID"
echo "BUILD_ID: $BUILD_ID_VAL"
chmod 0644 "$PKGROOT/usr/share/doc/$NAME/copyright"

# 去掉缓存与测试残留
find "$PKGROOT/usr/lib/xiaohuang-ofd" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKGROOT/usr/lib/xiaohuang-ofd" -name '*.pyc' -delete 2>/dev/null || true
rm -rf "$PKGROOT/usr/lib/xiaohuang-ofd/web/__pycache__"

# ---------------------------------------------------------------- 启动器
cat > "$PKGROOT/usr/bin/ofd-editor" <<'LAUNCHER'
#!/bin/sh
# OFD 桌面端启动器
export PYTHONPATH=/usr/lib/xiaohuang-ofd${PYTHONPATH:+:$PYTHONPATH}
exec python3 -m app.main "$@"
LAUNCHER
chmod 0755 "$PKGROOT/usr/bin/ofd-editor"

cat > "$PKGROOT/usr/bin/ofd-web" <<'LAUNCHER'
#!/bin/sh
# OFD 网页端启动器。默认 127.0.0.1:8760，加 --host 0.0.0.0 才对外。
export PYTHONPATH=/usr/lib/xiaohuang-ofd${PYTHONPATH:+:$PYTHONPATH}
exec python3 /usr/lib/xiaohuang-ofd/web/server.py "$@"
LAUNCHER
chmod 0755 "$PKGROOT/usr/bin/ofd-web"

cat > "$PKGROOT/usr/bin/ofd-cli" <<'LAUNCHER'
#!/bin/sh
export PYTHONPATH=/usr/lib/xiaohuang-ofd${PYTHONPATH:+:$PYTHONPATH}
exec python3 /usr/lib/xiaohuang-ofd/tools/ofd_cli.py "$@"
LAUNCHER
chmod 0755 "$PKGROOT/usr/bin/ofd-cli"

# ---------------------------------------------------------------- 桌面项
cat > "$PKGROOT/usr/share/applications/ofd-editor.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=小黄OFD
Name[en]=Xiaohuang OFD
GenericName=OFD 阅读器与编辑器
Comment=阅读、编辑、打印 OFD 版式文档
Comment[en]=Read, edit and print OFD documents
Exec=/usr/bin/ofd-editor %f
Icon=xiaohuang-ofd
Terminal=false
Categories=Office;Viewer;
MimeType=application/ofd;
StartupNotify=true
StartupWMClass=ofd-editor
DESKTOP
chmod 0644 "$PKGROOT/usr/share/applications/ofd-editor.desktop"

# 图标：用 Pillow 画一个，没有 Pillow 就跳过（用系统 fallback 图标）
python3 - "$PKGROOT/usr/share/icons/hicolor/256x256/apps/xiaohuang-ofd.png" <<'PYICON' || true
import sys
try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    sys.exit(0)

path = sys.argv[1]
S = 256
BLUE = (0, 114, 198, 255)
WHITE = (255, 255, 255, 255)
GREY = (150, 170, 190, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([10, 10, S - 10, S - 10], radius=38, fill=BLUE)
d.rounded_rectangle([58, 32, S - 58, 192], radius=8, fill=WHITE)
for i, y in enumerate(range(60, 180, 24)):
    w = 100 if i % 3 else 64
    d.rectangle([80, y, 80 + w, y + 6], fill=GREY)

# 品牌名放底部蓝底上写白字；中文字体找不到时退成 XH-OFD，避免画成方块
label = "\u5c0f\u9ec4OFD"
f = None
for cand in ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
             "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
             "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"):
    try:
        f = ImageFont.truetype(cand, 30)
        break
    except Exception:
        f = None
if f is None:
    label = "XH-OFD"
    try:
        f = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
    except Exception:
        f = ImageFont.load_default()
d.text((S // 2, 222), label, font=f, fill=WHITE, anchor="mm")
img.save(path)
PYICON

# MIME 类型注册
mkdir -p "$PKGROOT/usr/share/mime/packages"
cat > "$PKGROOT/usr/share/mime/packages/xiaohuang-ofd.xml" <<'MIME'
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/ofd">
    <comment>OFD 版式文档</comment>
    <comment xml:lang="en">OFD document</comment>
    <magic priority="60">
      <match type="string" value="PK\x03\x04" offset="0"/>
    </magic>
    <glob pattern="*.ofd" weight="80"/>
  </mime-type>
</mime-info>
MIME
chmod 0644 "$PKGROOT/usr/share/mime/packages/xiaohuang-ofd.xml"

# ---------------------------------------------------------------- control
INSTALLED_SIZE=$(du -sk "$PKGROOT" | cut -f1)
cat > "$PKGROOT/DEBIAN/control" <<CONTROL
Package: $NAME
Version: $VERSION
Section: office
Priority: optional
Architecture: $ARCH
Installed-Size: $INSTALLED_SIZE
Maintainer: Xiao Huang OFD <1924053793@qq.com>
Homepage: https://github.com/MrY551/xiaohuangOFD
Depends: python3 (>= 3.7), python3-pil, cups-client
Recommends: python3-pyside6.qtcore | python3-pyside2.qtcore | python3-pyqt5, fonts-noto-cjk
Suggests: ghostscript
Description: 小黄OFD - OFD 版式文档阅读、编辑与打印工具
 纯 Python 实现的 OFD（GB/T 33190-2016）处理工具，含桌面端、网页端与命令行。
 支持文本/路径/图片对象渲染、文字与样式编辑、页管理、N 合 1 拼版与 CUPS 打印，
 可导出 PDF / PNG / SVG / 纯文本。桌面端基于 Qt，网页端在浏览器 Canvas 上渲染，
 二者与命令行共用同一套核心库。
CONTROL
chmod 0644 "$PKGROOT/DEBIAN/control"

# ---------------------------------------------------------------- 维护脚本
cat > "$PKGROOT/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    update-mime-database /usr/share/mime >/dev/null 2>&1 || true
    gtk-update-icon-cache -q /usr/share/icons/hicolor >/dev/null 2>&1 || true
    update-desktop-database >/dev/null 2>&1 || true
fi
exit 0
POSTINST
chmod 0755 "$PKGROOT/DEBIAN/postinst"

cat > "$PKGROOT/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    update-mime-database /usr/share/mime >/dev/null 2>&1 || true
    update-desktop-database >/dev/null 2>&1 || true
fi
exit 0
POSTRM
chmod 0755 "$PKGROOT/DEBIAN/postrm"

# ---------------------------------------------------------------- 构建
OUT="$DIST/${NAME}_${VERSION}_${ARCH}.deb"
( cd "$PKGROOT" && find . -type f -exec md5sum {} + > "$PKGROOT/DEBIAN/md5sums" ) || true
dpkg-deb --root-owner-group --build "$PKGROOT" "$OUT" >/dev/null
echo "== 产物: $OUT"
dpkg-deb -I "$OUT" | sed 's/^/   /'
ls -lh "$OUT" | awk '{print "   大小:", $5}'

if [ "${1:-}" = "--deps" ]; then
    echo
    echo "== 运行时依赖检查"
    python3 - <<'PYCHECK'
import importlib, sys
mods = [("PIL", "python3-pil"), ("PySide6", "python3-pyside6 · 或 pip install PySide6"),
        ("PySide2", "python3-pyside2 · 或 pip install PySide2"),
        ("PyQt5", "python3-pyqt5 · 或 pip install PyQt5")]
ok_qt = False
for name, pkg in mods:
    try:
        m = importlib.import_module(name)
        print("  [有] %-10s %-12s" % (name, getattr(m, "__version__", "")))
        if name in ("PySide6", "PySide2", "PyQt5"):
            ok_qt = True
    except Exception:
        print("  [缺] %-10s 需要: %s" % (name, pkg))
if not ok_qt:
    print("  ! 桌面端需要至少一个 Qt 绑定；网页端与命令行不需要 Qt")
PYCHECK
fi

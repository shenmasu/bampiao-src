#!/bin/bash
# 在 macOS 上打出「半瓢.app」和 dmg。必须在仓库根目录跑（或用绝对路径）。
#
#   .venv/bin/python 需要已装好依赖：uv venv .venv && uv pip install -e .
#
# 为什么要手工拼 .app：PyInstaller 的 BUNDLE 只认一个主可执行文件，
# 而这个项目有 GUI 和 CLI 两个（面板要能替用户起 login 子进程）。

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

# 版本号只认 __init__.py 一处：以前这里写死 0.1.0，发 0.2.0 时 dmg 和 Info.plist 还叫 0.1.0
# tr -d '\r' 不能省：Windows 上编辑的 __init__.py 是 CRLF，sed 读出来的版本号会带一个回车，
# 于是产物叫 "半瓢-0.2.1\r.dmg" —— scp 找不到、dmg 名字里藏着一个不可见字符（2026-10-30 真踩）。
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' image2_bampiao/__init__.py | tr -d '\r')"
APP_NAME="半瓢"
APP="dist/${APP_NAME}.app"
PYI=".venv/bin/pyinstaller"

if [ ! -x "$PYI" ]; then
  echo "找不到 $PYI，先建虚拟环境：uv venv .venv --python 3.12 && uv pip install -e ." >&2
  exit 1
fi

echo "== 1/5 生成 .icns"
if [ -f packaging/bampiao_1024.png ]; then
  rm -rf build/icon.iconset
  mkdir -p build/icon.iconset
  for s in 16 32 64 128 256 512; do
    sips -z "$s" "$s" packaging/bampiao_1024.png \
      --out "build/icon.iconset/icon_${s}x${s}.png" >/dev/null
    d=$((s * 2))
    sips -z "$d" "$d" packaging/bampiao_1024.png \
      --out "build/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns build/icon.iconset -o packaging/bampiao.icns
  echo "   packaging/bampiao.icns"
else
  echo "   跳过（没有 packaging/bampiao_1024.png）"
fi

echo "== 2/5 PyInstaller 打包（mac.spec 里的 BUNDLE 直接出 ${APP_NAME}.app）"
rm -rf build/bampiao dist/bampiao "$APP"
"$PYI" --noconfirm --clean packaging/mac.spec 2>&1 | tail -3

echo "== 3/5 整包 ad-hoc 签名 + 校验封印"
# 由 BUNDLE 出的包结构是 codesign 认的（Frameworks 里只有 Mach-O），可以整包签。
# 校验不过就停：签名坏了的包发出去，用户看到的是「已损坏」，比不签还糟。
codesign --force --sign - --timestamp=none "$APP"
codesign --verify --strict "$APP"
echo "   签名有效（ad-hoc；没有 Developer ID，首次打开要在「隐私与安全性」里点「仍要打开」）"

echo "== 4/5 自检（会不会缺文件）"
"$APP/Contents/MacOS/bampiao" --version

echo "== 5/5 打 dmg"
STAGE="dist/dmg-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -sfn /Applications "$STAGE/Applications"
rm -f "dist/${APP_NAME}-${VERSION}.dmg"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$STAGE" \
  -ov -format UDZO "dist/${APP_NAME}-${VERSION}.dmg"

echo
echo "完成："
du -sh "$APP" "dist/${APP_NAME}-${VERSION}.dmg"

# 2026-11-06：.app（约 600M）和 dmg-stage 打完就没用了。
# 以前每打一版都留着，配上"重打前先备份"的习惯，五次之后 dist-prev-* 攒到 3.4G。
# dmg 在就算交付物在；下次打包第 2 步本来也会 rm -rf 重建。
rm -rf "$STAGE" "$APP"
echo "   （已清掉 .app 和临时挂载目录，只留 dmg）"

# 顺手把更早的 bp-* 打包目录也收掉（保留当前这份）。清失败不影响打包结果。
if [ -x "$HERE/clean_mac.sh" ]; then
  KEEP=1 bash "$HERE/clean_mac.sh" --apply 2>/dev/null | tail -3 || true
fi

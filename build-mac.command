#!/bin/bash
# 半瓢 · macOS 一键打包
#
# 用法：双击本文件。如果双击提示「没有适当的访问权限」，在终端里跑：
#     chmod +x build-mac.command && ./build-mac.command
#
# 这个脚本只做三件事：装依赖 → 打包 → 签名并自检。不联网上传、不改系统设置。
# 打完的 dmg 在 dist/ 里，自己能用，也可以直接发给别人。

set -uo pipefail
cd "$(dirname "$0")" || exit 1
LOG="$(pwd)/build.log"
: > "$LOG"

say()  { printf "\n\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "   ✅ %s\n" "$*"; }
die()  { printf "\n❌ %s\n\n完整日志：%s\n按回车关闭窗口…" "$*" "$LOG"; read -r _; exit 1; }

say "半瓢 · macOS 一键打包"
printf "   工作目录：%s\n" "$(pwd)"

# ---------------------------------------------------------------- 1. 这台机器是什么架构
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) TAG="x64";   say "1/7 这台是 Intel Mac → 打出 x64 包（Intel 用户能用）" ;;
  arm64)  TAG="arm64"; say "1/7 这台是 Apple 芯片 → 打出 arm64 包（只有 M 系列能用）" ;;
  *) die "不认识的架构：$ARCH" ;;
esac

# ---------------------------------------------------------------- 2. 找一个 3.12+ 且架构对得上的 Python
# 不能只认 python3：机器上可能同时有 Xcode 自带的 3.9（叫 python3）和用户装的 3.12（叫 python3.12）。
# 只认 python3 会先撞上 3.9，然后告诉"你已经装了 3.12"的人"去装 Python"，纯属误导。
PY=""
TOO_OLD=""
WRONG_ARCH=""
for c in python3 python3.13 python3.12 python3.11 python3.10; do
  command -v "$c" >/dev/null 2>&1 || continue
  /usr/bin/env "$c" - <<'PYV' >/dev/null 2>&1 || { TOO_OLD="$TOO_OLD $c"; continue; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PYV
  parc="$(/usr/bin/env "$c" -c 'import platform;print(platform.machine())' 2>/dev/null)"
  [ "$parc" = "$ARCH" ] || { WRONG_ARCH="$WRONG_ARCH $c($parc)"; continue; }
  PY="$c"; break
done

if [ -z "$PY" ]; then
  if [ -n "$WRONG_ARCH" ]; then
    die "找到的 Python 架构和这台机器（$ARCH）不一致：$WRONG_ARCH
   装 python.org 的官方安装包（别用 Homebrew），它会跟你系统架构一致。"
  fi
  if [ -n "$TOO_OLD" ]; then
    die "找到的 Python 太老（需要 3.12 以上），只看到：$TOO_OLD
   去 https://www.python.org/downloads/ 装最新的 macOS 安装包。"
  fi
  die "没找到 python3。去 https://www.python.org/downloads/ 装最新的 macOS 安装包。"
fi
printf "   Python：%s（%s）\n" "$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')" "$ARCH"
ok "Python 够用"

# ---------------------------------------------------------------- 3. 虚拟环境 + 依赖
if [ ! -x .venv-build/bin/python ]; then
  say "2/7 建虚拟环境（第一次会慢一点）"
  "$PY" -m venv .venv-build >>"$LOG" 2>&1 || die "建虚拟环境失败"
fi
# shellcheck disable=SC1091
. .venv-build/bin/activate

say "3/7 装依赖（要联网，第一次约 3-5 分钟；下面会滚动输出，不是卡死）"
python -m pip install --upgrade pip 2>&1 | tee -a "$LOG" | tail -3
python -m pip install -e . pyinstaller 2>&1 | tee -a "$LOG" | tail -8 \
  || die "依赖装不上。把上面几行，或者 build.log 整段发我。"
python - <<'PY' 2>&1 | tee -a "$LOG" || die "依赖装好了但导入失败，把报错发我"
import PyInstaller, webview, PIL, numpy, cryptography, playwright  # noqa: F401
print("   依赖齐了：PyInstaller %s" % PyInstaller.__version__)
PY
ok "依赖就绪"

# ---------------------------------------------------------------- 4. 图标
say "4/7 生成图标"
if [ -f packaging/bampiao_1024.png ]; then
  rm -rf build/icon.iconset && mkdir -p build/icon.iconset
  for s in 16 32 64 128 256 512; do
    sips -z "$s" "$s" packaging/bampiao_1024.png --out "build/icon.iconset/icon_${s}x${s}.png" >/dev/null 2>&1
    d=$((s * 2))
    sips -z "$d" "$d" packaging/bampiao_1024.png --out "build/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null 2>&1
  done
  if iconutil -c icns build/icon.iconset -o packaging/bampiao.icns 2>>"$LOG"; then
    ok "packaging/bampiao.icns"
  else
    printf "   （图标没生成，会退回默认图标，不影响使用）\n"
  fi
else
  printf "   （没有 bampiao_1024.png，跳过）\n"
fi

# ---------------------------------------------------------------- 5. 打包
say "5/7 开始打包（3-8 分钟，这个窗口别关）"
rm -rf build/bampiao "dist/半瓢.app"
python -m PyInstaller --noconfirm --clean packaging/mac.spec 2>&1 | tee -a "$LOG" | tail -10
APP="dist/半瓢.app"
[ -d "$APP" ] || die "打包失败，没有生成 $APP。把 build.log 发我（路径在上面的提示里）。"

# ---------------------------------------------------------------- 6. 签名 + 自检
say "6/7 签名并校验"
codesign --force --sign - --timestamp=none "$APP" >>"$LOG" 2>&1 || die "签名失败"
codesign --verify --strict "$APP" >>"$LOG" 2>&1 \
  || die "签名校验没过——这个包发出去用户会看到「已损坏」，绝对不能发。"
xattr -cr "$APP"
ok "签名有效（ad-hoc；没有苹果开发者证书，首次打开要在「隐私与安全性」里点「仍要打开」）"
printf "   包里主程序的架构："
lipo -archs "$APP/Contents/MacOS/bampiao"
"$APP/Contents/MacOS/bampiao" --version || die "打出来的程序跑不起来"
ok "自检通过"

# ---------------------------------------------------------------- 7. dmg
say "7/7 打 dmg"
VERSION="$(python - <<'PY'
import re, pathlib
s = pathlib.Path("image2_bampiao/__init__.py").read_text(encoding="utf-8")
m = re.search(r'__version__\s*=\s*"([^"]+)"', s)
print(m.group(1) if m else "0.0.0")
PY
)"
STAGE="dist/dmg-stage"
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -sfn /Applications "$STAGE/Applications"
DMG="dist/半瓢-${VERSION}-${TAG}.dmg"
rm -f "$DMG"
hdiutil create -quiet -volname "半瓢" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >>"$LOG" 2>&1 \
  || die "打 dmg 失败"
# 清干净：dist/bampiao 是 COLLECT 的中间目录（193M），半瓢.app 已经在 dmg 里了。
# 留着它们会让 dist 变成三样东西，朋友容易发错（实测就是这么发现的）。
rm -rf "$STAGE" dist/bampiao "$APP"

printf "\n────────────────────────────────────────\n"
printf "完成 ✅\n\n"
printf "   安装包：%s\n" "$(pwd)/$DMG"
printf "   大小：  %s\n" "$(du -h "$DMG" | cut -f1)"
printf "   版本：  %s（%s）\n" "$VERSION" "$TAG"
printf "   SHA256：%s\n" "$(shasum -a 256 "$DMG" | cut -d' ' -f1)"
printf "\n   dist/ 里现在只有这一个 dmg，发它就行。\n"
printf "   自己要用：双击 dmg，把「半瓢」拖进「应用程序」；\n"
printf "   首次打开提示「无法验证开发者」→ 系统设置 → 隐私与安全性 → 仍要打开。\n"
printf "   完整日志：%s\n" "$LOG"
printf "────────────────────────────────────────\n\n"

open -R "$DMG" 2>/dev/null || true
printf "按回车关闭窗口…"
read -r _ || true
exit 0

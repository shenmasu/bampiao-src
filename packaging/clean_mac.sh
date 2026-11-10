#!/bin/bash
# 清 Mac 上攒下来的打包目录。默认只列清单，加 --apply 才真删。
#
# 为什么要有这个（2026-11-06 盘账）：Mac 家目录里 8.8G，7G 是可重生成的产物 ——
# 五次"重打前备份"的 dist-prev-*（3.4G）、14 个老 bp-0xxx（1.1G）、
# 还有一个 1.5G 的 dist（.app + dmg 打完就没人用了）。
#
#   bash packaging/clean_mac.sh              # 只列
#   bash packaging/clean_mac.sh --apply      # 真删
#   KEEP=2 bash packaging/clean_mac.sh --apply
#
# 保留：~/bp-<最新>、~/bampiao-src 的源码和 .venv、所有 dmg。
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
KEEP="${KEEP:-1}"
APPLY=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --keep=*) KEEP="${a#--keep=}" ;;
  esac
done

del() {
  local p="$1"
  [ -e "$p" ] || return 0
  local sz
  sz="$(du -sh "$p" 2>/dev/null | cut -f1)"
  if [ "$APPLY" = "1" ]; then
    rm -rf "$p" && echo "  删 $sz  $p"
  else
    echo "  $sz  $p"
  fi
}

echo "== 老的 bp-* 打包目录（保留最近 $KEEP 个）"
# 按修改时间倒序；跳过最新 KEEP 个，也跳过当前仓库所在的目录
cur="$(cd "$ROOT" && pwd -P)"
i=0
while IFS= read -r d; do
  [ -z "$d" ] && continue
  case "$(cd "$d" && pwd -P)" in
    "$cur"|"$cur"/*) continue ;;        # 正在用的仓库，不碰
  esac
  i=$((i + 1))
  [ "$i" -le "$KEEP" ] && continue
  del "$d"
done < <(ls -dt "$HOME"/bp-[0-9]* 2>/dev/null)

echo "== 验证/临时目录"
for d in "$HOME"/bp-verify-* "$HOME"/bp-vis; do
  [ -d "$d" ] && del "$d"
done

echo "== 打包中间产物"
for d in "$HOME"/bampiao-src/image2-bampiao/dist-prev-* \
         "$HOME"/bampiao-src/image2-bampiao/build-prev-*; do
  [ -d "$d" ] && del "$d"
done
# 每个 bp-* 目录里的 dist（.app 一两百兆）和 build，dmg 已经单独存着，不需要留。
# 当前仓库所在的目录跳过：正在打包时把 build/ 删了会直接失败。
for d in "$HOME"/bp-[0-9]*/src/dist "$HOME"/bp-[0-9]*/src/build; do
  [ -d "$d" ] || continue
  case "$d" in "$cur"/*) continue ;; esac
  del "$d"
done

echo "== 老源码副本里的 .venv / .devdata"
# 这两个是 0.2.3 / 0.2.25 的旧副本，各带一份 250M venv + 215M 开发 profile。
# 打包只用 bp-* 那个目录，这两份重建一次就没意义了；源码本身留着。
for d in "$HOME"/bampiao-src/image2-bampiao/.venv "$HOME"/bampiao-src/image2-bampiao/.devdata \
         "$HOME"/image2-bampiao/.venv "$HOME"/image2-bampiao/.devdata; do
  [ -d "$d" ] || continue
  case "$d" in "$cur"/*) continue ;; esac
  del "$d"
done

echo
if [ "$APPLY" = "1" ]; then
  echo "完成。现在：$(df -h / | tail -1 | awk '{print $4 " 可用"}')"
else
  echo "只是看看。要真删：bash packaging/clean_mac.sh --apply"
fi

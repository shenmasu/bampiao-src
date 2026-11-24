#!/bin/sh
set -eu

KEY="${1:-${BAMPIAO_KEY:-}}"
if [ -z "$KEY" ]; then
  printf '%s\n' '缺少半瓢密钥。请通过参数或 BAMPIAO_KEY 提供。' >&2
  exit 1
fi

URL='https://hezhuzi.com/bp/skill/bampiao-skill.zip'
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/src" "$HOME/.bampiao"

if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$URL" -o "$TMP/bampiao-skill.zip"
else
  wget -q "$URL" -O "$TMP/bampiao-skill.zip"
fi

if command -v python3 >/dev/null 2>&1; then
  python3 -m zipfile -e "$TMP/bampiao-skill.zip" "$TMP/src"
else
  unzip -q "$TMP/bampiao-skill.zip" -d "$TMP/src"
fi

STAMP="$(date +%Y%m%d%H%M%S)"
install_one() {
  base="$1"
  [ -d "$base" ] || return 0
  dest="$base/skills/bampiao"
  mkdir -p "$base/skills"
  if [ -e "$dest" ]; then mv "$dest" "$dest.previous-$STAMP"; fi
  cp -R "$TMP/src" "$dest"
}

for base in \
  "$HOME/.workbuddy" "$HOME/.hermes" "$HOME/.claude" "$HOME/.codex" \
  "$HOME/.cursor" "$HOME/.openclaw" "$HOME/.windsurf" "$HOME/.trae" \
  "$HOME/.gemini/antigravity" "$HOME/.agents" "$HOME/.dsh"
do
  install_one "$base"
done

if [ -e "$HOME/.bampiao/skill" ]; then
  mv "$HOME/.bampiao/skill" "$HOME/.bampiao/skill.previous-$STAMP"
fi
cp -R "$TMP/src" "$HOME/.bampiao/skill"
umask 077
printf '%s\n' "$KEY" > "$HOME/.bampiao/key"
chmod 600 "$HOME/.bampiao/key"

if command -v python3 >/dev/null 2>&1; then
  python3 "$HOME/.bampiao/skill/scripts/bampiao_draw.py" --check
elif command -v node >/dev/null 2>&1; then
  node "$HOME/.bampiao/skill/scripts/bampiao_draw.mjs" --check
else
  printf '%s\n' '技能已安装，但本机没有 Python 3 或 Node 18+，自检未运行。' >&2
  exit 2
fi

printf '%s\n' '安装完成。以后新开任意窗口，说“用半瓢生图”即可。'

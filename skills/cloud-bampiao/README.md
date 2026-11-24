# 半瓢云端生图 Skill

安装后，新开任意支持 `SKILL.md` 的 Agent 窗口，说“用半瓢生图”即可。

密钥保存在 `~/.bampiao/key`。环境变量 `BAMPIAO_URL` 可覆盖默认地址 `https://hezhuzi.com`。

```bash
python3 scripts/bampiao_draw.py --check
python3 scripts/bampiao_draw.py "一只白鹭穿过晨雾，极简留白，无文字" --ratio 16:9
```

没有 Python 时：

```bash
node scripts/bampiao_draw.mjs --check
node scripts/bampiao_draw.mjs "一只白鹭穿过晨雾，极简留白，无文字" --ratio 16:9
```

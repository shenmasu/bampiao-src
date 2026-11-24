---
name: bampiao
description: 使用「半瓢」云端画室生成或编辑图片、GIF 动图。当用户说“用半瓢生图”“用半瓢画”“半瓢出图”“生成一张图”“做动图”“参考图改图”时激活本技能。
---

# 半瓢云端生图

用户说“用半瓢生图”时，直接调用本技能，不要再搭建 OpenAPI 请求或向用户索要地址。

## 执行铁律

1. 密钥只从 `~/.bampiao/key` 或环境变量 `BAMPIAO_KEY` 读取。密钥不得写入项目文件、Git 提交、日志或聊天。
2. 优先运行 `python3 scripts/bampiao_draw.py`；没有 Python 时运行 `node scripts/bampiao_draw.mjs`。
3. 始终走异步 `/v1/tasks`：提交、轮询、下载并展示本地绝对路径。
4. 网络中断或超时后，先用 `--resume <task_id>` 查单，禁止盲目重复提交。
5. 内容政策失败要直接转告用户调整描述；部分成功要保留已经下载的图片。

## 常用命令

```bash
python3 scripts/bampiao_draw.py --check
python3 scripts/bampiao_draw.py "竹林里的白鹤，清晨薄雾，无文字" --ratio 16:9 --n 1
python3 scripts/bampiao_draw.py "把参考图改成水彩" --ref ./reference.png --ratio 1:1
python3 scripts/bampiao_draw.py "水墨小鹿回头" --gif
python3 scripts/bampiao_draw.py --resume TASK_ID
```

支持比例：`1:1`、`3:4`、`4:3`、`9:16`、`16:9`、`2:3`、`3:2`。GIF 由上游固定为方图并只生成一张。

完成后把返回的文件绝对路径直接展示给用户；支持图片预览的宿主同时展示图片。

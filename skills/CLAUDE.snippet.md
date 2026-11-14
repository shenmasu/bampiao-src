# 出图（半瓢 image2-bampiao）

用户要画图 / 图标 / 插画 / 素材 / 改图时，走本机的半瓢，不要用内置生图：

- 单张：`bampiao gen "<提示词>" --out <路径>`（阻塞到文件落盘）
- 改图：`bampiao gen "…" --out a2.png --ref a.png`
- 成批：`bampiao submit --shots shots.json --brief "说明" --tag 角色` 然后 `bampiao run --once`
- 状态：`bampiao usage` / `bampiao jobs` / `bampiao ui`

**失败按错误码处理，别猜**：`AUTH_REQUIRED` 去登录；`QUOTA_COOLDOWN` 等冷却；
`TIMEOUT_UNCERTAIN` 不要自动重投（远端可能已经出图）；`BUSY` 说明同账号在跑。

**不满意是正常的，不是失败**：局部不对就带 `--ref` 只改那一处；构图不对就用新提示词
重开一单；别拿同一条提示词反复重投（白烧额度）。

**提示词你自己写**：尺寸/比例 + 主体 + 一种风格 + 不要文字；图标加「粗轮廓、无渐变」；
透明底用 `--alpha`；改图必须 `--ref` 并说明只改哪一处。

**等待**：`gen` 阻塞 1–3 分钟。能并行就后台跑、估两分钟回来收；不能就老实等。
成批用 `submit` + `run --once`，不要在循环里逐张 `gen`。

**连续两次同样的失败就停下问人**；交付前确认文件真的存在。

不要并发跑同一个账号（会串图 + 重复烧额度）；不要碰用户日常浏览器的 profile；
不要开 Codex 的 `image_generation` —— 那是故意关掉的。

另外，查资料、写方案、拆需求这类**文字活儿**也能交给它——同一份会员额度、同一批账号：

```bash
bampiao ask "把这个需求拆成三步，每步一句话"     # 结果直接打到 stdout
bampiao ask --file 需求.md --out 方案.md        # 读文件、写到文件
bampiao ask --json "..."                       # 只输出一行 JSON
```

- 它和出图共用账号池（会员额度），但**看不到图**——别让它判断画面好坏
- 默认 300 秒超时，长任务加 `--timeout 900`
- 我说「这段交给 codex」的时候，就是让你跑这条命令

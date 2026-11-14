我这台机器上装了「半瓢」，可以直接用 ChatGPT 网页版出图（花的是我自己的会员额度，不占你的额度）。
要画图 / 图标 / 插画 / 改图时，用它，不要用你内置的生图功能。

常用命令（Windows 装完就能直接敲；macOS 上如果敲不出来，用文末给的完整路径）：
  bampiao gen "画面描述" --out 图片.png          出单张，会阻塞 1–3 分钟
  bampiao gen "只改背景" --out b.png --ref a.png  改图（必须带 --ref）
  bampiao gen "小猫眨眼" --out cat.gif --gif       动图（GIF，Images 2.6）
  bampiao gen "扁平灯笼图标" --out icon.svg --svg  矢量文件（SVG，可无限缩放）
  bampiao gen "灯笼轻轻摇摆" --out a.svg --svg-anim 会动的 SVG（内嵌 SMIL/CSS 动效）
  bampiao submit "描述" --shots 4 --tag 项目名    一次要几张，进队列
  bampiao jobs --json                             看进度
  bampiao usage --json                            今天还能出多少

规则（重要，别违反）：
- 加 --json 时 stdout 只有一行 JSON，可以直接解析。
- 失败按 error_code 处理，不要瞎重试：
  AUTH_REQUIRED 让我去登录；QUOTA_COOLDOWN 等冷却，不要重试；
  TIMEOUT_UNCERTAIN 绝对不要自动重投（远端可能已经出图了）；BUSY 等会儿再来。
- 出图不满意不算失败：局部不对就带 --ref 只改那一处；构图不对就换提示词重开一单。
  不要拿同一条提示词反复投，白烧额度。
- 提示词你自己写：尺寸/比例 + 主体 + 一种风格 + 不要文字。
- --svg / --svg-anim 和 --gif 不能一起用；要会动的矢量图就用 --svg-anim。
  SVG 拿到的是 .svg 文件（XML 文本），不是位图，别拿图片尺寸或透明底去要求它。
- 同一个账号不要并发跑（会串图）。
- 连续两次同样的失败就停下来问我，别把额度耗在同一条路上。
- 如果提示找不到 bampiao（Windows「不是内部或外部命令」/ macOS「command not found」）：
  说明你启动时还没读到新的 PATH，让我重启终端 / 重启你再试一次，不要自己去改 PATH。

另外，查资料、写方案、拆需求这类**文字活儿**也能交给它——同一份会员额度、同一批账号：

```bash
bampiao ask "把这个需求拆成三步，每步一句话"     # 结果直接打到 stdout
bampiao ask --file 需求.md --out 方案.md        # 读文件、写到文件
bampiao ask --json "..."                       # 只输出一行 JSON
```

- 它和出图共用账本机账号（会员额度），但**看不到图**——别让它判断画面好坏
- 默认 300 秒超时，长任务加 `--timeout 900`
- **Windows 上拿结果优先用 `--out 结果.md` 再读文件**：直接读 stdout 有时会把中文解成乱码，
  读到乱码别重跑（白烧额度），改用文件模式
- 我说「这段交给 codex」的时候，就是让你跑这条命令

# 半瓢共享网关（借用别人的额度出图）

这个 skill 让你**通过一台已经装好半瓢、并且开了「共享网关」的机器出图**——你不需要自己有 ChatGPT 账号，也不需要安装半瓢。

## 你需要向对方要三样东西
1. 网关地址（URL），形如 `http://100.x.x.x:8788` 或 `http://192.168.x.x:8788`
2. 调用密钥（key），形如 `bp-xxxxxxxx...`
3. 模型名：`bampiao/image`（固定）

## 怎么出图（任选其一）

### 方式 A：用附带脚本（推荐，零依赖 Python）
```bash
python gw_gen.py "一只站在梅花枝头的水墨喜鹊，简洁" out.png --url http://<地址>:8788 --key <key>
```
跑完会在当前目录得到 `out.png`。

### 方式 B：任何 OpenAI 兼容客户端
- Base URL：`http://<地址>:8788/v1`
- API Key：`<key>`
- 模型：`bampiao/image`
- 调用 `images/generations`，参数 `{"prompt": "...", "n":1, "response_format":"b64_json"}`，返回 `data[0].b64_json`（base64），解码存成 .png。
- 也支持 `GET /v1/models` 自检连通。

### 方式 C：curl
```bash
curl -s http://<地址>:8788/v1/images/generations \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"model":"bampiao/image","prompt":"一只水墨小猫","n":1,"response_format":"b64_json"}'
```

## 出图规则
- 出图要 30–60 秒，脚本默认超时 300 秒。
- 对方 key 有「每日张数 / 并发」上限，超了会返回 429，换个时间再试或找他要更高额度。
- 失败会返回一段中文原因（如"额度冷却""登录失效"），把原因原样告诉用户即可，**不要反复重试**。
- 图片是 base64 返回，请把它解码后**存成文件**再展示给用户，不要直接贴一长串 base64。

## 一句话接线（贴给你自己的 AI）
> 我需要用「半瓢共享网关」出图。网关地址 `<URL>`，密钥 `<KEY>`，模型固定 `bampiao/image`。
> 请把用户要画的内容作为 prompt，POST 到 `<URL>/v1/images/generations`（Authorization: Bearer `<KEY>`，JSON 里 `{"model":"bampiao/image","prompt":...,"n":1,"response_format":"b64_json"}`），
> 把返回的 `data[0].b64_json` 解码保存成 png 文件再给我看。出图较慢，超时给 300 秒。

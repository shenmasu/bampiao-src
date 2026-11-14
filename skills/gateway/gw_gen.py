#!/usr/bin/env python3
"""半瓢共享网关的最小客户端（零依赖）。

用法：
    python gw_gen.py "一只水墨小猫" out.png --url http://100.x.x.x:8788 --key bp-xxxx

也可以把地址/密钥放环境变量：
    set BAMPIAO_GW_URL=http://100.x.x.x:8788
    set BAMPIAO_GW_KEY=bp-xxxx
    python gw_gen.py "一只水墨小猫" out.png
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("out", nargs="?", default="bampiao_gw.png")
    ap.add_argument("--url", default=os.environ.get("BAMPIAO_GW_URL", "http://127.0.0.1:8788"))
    ap.add_argument("--key", default=os.environ.get("BAMPIAO_GW_KEY", ""))
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=300)
    a = ap.parse_args()
    if not a.key:
        sys.exit("缺少 --key（或环境变量 BAMPIAO_GW_KEY）")
    body = json.dumps({"model": "bampiao/image", "prompt": a.prompt, "n": a.n,
                       "response_format": "b64_json"}).encode("utf-8")
    req = urllib.request.Request(a.url.rstrip("/") + "/v1/images/generations", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + a.key})
    try:
        resp = urllib.request.urlopen(req, timeout=a.timeout)
        j = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit("网关返回 %s：%s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
    except Exception as e:  # noqa: BLE001
        sys.exit("连不上网关：%s" % e)
    data = j.get("data") or []
    if not data:
        sys.exit("网关没返回图：%s" % json.dumps(j, ensure_ascii=False)[:300])
    base, ext = os.path.splitext(a.out)
    for i, item in enumerate(data):
        raw = base64.b64decode(item.get("b64_json") or "")
        p = a.out if len(data) == 1 else "%s_%d%s" % (base, i + 1, ext or ".png")
        with open(p, "wb") as f:
            f.write(raw)
        print("已保存：%s（%d 字节）" % (p, len(raw)))


if __name__ == "__main__":
    main()

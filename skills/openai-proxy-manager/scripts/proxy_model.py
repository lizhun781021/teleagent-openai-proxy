#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代理默认模型管理脚本
封装 TeleAgent OpenAI 兼容代理（8088）的默认模型查看/设置与模型列表功能。

用法:
    python3 proxy_model.py status           # 查看当前默认模型
    python3 proxy_model.py set <model>      # 设置默认模型，如 NewApi/chat-pro
    python3 proxy_model.py list             # 列出已配置的模型（Provider/模型）
"""

import json
import sys
import urllib.request

PROXY_URL = "http://127.0.0.1:8088"


def api_get(path):
    req = urllib.request.Request(f"{PROXY_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{PROXY_URL}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_status():
    d = api_get("/api/default-model")
    print(f"当前默认模型: {d.get('default_model', '未知')}")
    # 同时显示代理整体状态
    try:
        st = api_get("/api/status")
        proxy = st.get("proxy", {})
        print(f"代理状态: {proxy.get('status', '?')} (uptime {proxy.get('uptime_human', '?')})")
    except Exception as e:
        print(f"代理状态获取失败: {e}")


def cmd_set(model):
    if not model or "/" not in model:
        print(f"错误: 模型格式应为 provider/model，如 NewApi/chat-pro，收到: {model!r}")
        return 1
    d = api_post("/api/default-model", {"model": model})
    if d.get("success"):
        print(f"✓ 默认模型已设置为: {model}（立即生效，影响所有未指定 model 的请求）")
        return 0
    else:
        print(f"✗ 设置失败: {d.get('error', '未知错误')}")
        return 1


def cmd_list():
    d = api_get("/api/models")
    providers = d.get("providers", [])
    print(f"共 {len(providers)} 个已配置 Provider（未配置的内置目录不显示）:")
    for p in providers:
        pname = p.get("name", p.get("id", "?"))
        models = p.get("models", [])
        print(f"  [{pname}]")
        for m in models:
            mid = m.get("id", "?")
            print(f"    - {pname}/{mid}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "status":
        return cmd_status()
    elif cmd == "set":
        if len(sys.argv) < 3:
            print("用法: python3 proxy_model.py set <provider/model>")
            return 1
        return cmd_set(sys.argv[2])
    elif cmd == "list":
        return cmd_list()
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '45d2aa12-3fc8-47cc-935d-568824781825'
  PropagateID: '45d2aa12-3fc8-47cc-935d-568824781825'
  ReservedCode1: 'ece987f3-0418-43a2-bfa6-42790e9d54d5'
  ReservedCode2: 'ece987f3-0418-43a2-bfa6-42790e9d54d5'
---

# TeleAgent OpenAI-Compatible Proxy

将 [TeleAgent](https://teleagent.cn) 桌面应用的 super-agent API 转换为 **OpenAI 兼容接口**，任何支持 OpenAI API 的工具/SDK 均可直接接入。

## 特性

- **OpenAI 兼容**：标准 `/v1/chat/completions` 和 `/v1/models` 端点
- **流式 & 非流式**：支持 SSE 流式输出和一次性返回
- **多轮对话**：通过 messages 数组传入完整历史
- **自动签名**：自动从运行中的 scheduler 进程获取 session key
- **224 个模型**：支持 19 个 Provider（云端 + 本地）
- **Web 控制台**：内置仪表盘、模型管理、请求日志、在线测试、会话管理
- **持久化运行**：通过 macOS launchd 开机自启、崩溃自动重启
- **零依赖**：纯 Python 标准库实现，无需 pip install

## 快速开始

### 前置条件

- macOS（依赖 TeleAgent 桌面应用运行中的 super-agent）
- Python 3.8+

### 启动代理

```bash
python3 openai_proxy.py --port 8088 --host 0.0.0.0
```

### 调用示例

```bash
# 非流式
curl http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "NewApi/chat-pro",
    "messages": [{"role": "user", "content": "你好"}]
  }'

# 流式
curl -N http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "NewApi/chat-pro",
    "messages": [{"role": "user", "content": "讲个笑话"}],
    "stream": true
  }'

# 获取模型列表
curl http://127.0.0.1:8088/v1/models
```

### Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8088/v1",
    api_key="any"  # 随意填，不校验
)

resp = client.chat.completions.create(
    model="NewApi/chat-pro",
    messages=[{"role": "user", "content": "你好"}]
)
print(resp.choices[0].message.content)
```

## 接入第三方工具

| 配置项 | 值 |
|--------|-----|
| API Base URL | `http://127.0.0.1:8088/v1` |
| API Key | `any`（随意填写） |
| 模型名 | `NewApi/chat-pro` |

已验证可接入：OpenAI Python SDK、LangChain、Cursor、Continue、ChatBox、NextChat 等。

## Web 控制台

访问 `http://127.0.0.1:8088/console` 即可使用：

- **仪表盘**：实时统计（请求数/Token/错误率）、服务状态、模型调用统计
- **模型管理**：浏览全部 224 个模型，按 Provider 分组
- **请求日志**：查看最近 200 条请求的时间/模型/状态/耗时/Token
- **在线测试**：直接在页面发送请求并查看响应
- **会话管理**：查看 super-agent 创建的会话列表
- **系统信息**：版本、签名机制、API 端点文档
- **使用说明**：内置完整使用文档，含代码示例和一键复制

## 持久化运行（macOS launchd）

```bash
# 创建 launchd 配置
cat > ~/Library/LaunchAgents/com.lizhun.openai-proxy.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lizhun.openai-proxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/YOUR_USERNAME/scripts/openai_proxy.py</string>
        <string>--port</string>
        <string>8088</string>
        <string>--host</string>
        <string>0.0.0.0</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/openai-proxy.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/openai-proxy.log</string>
</dict>
</plist>
EOF

# 加载服务
launchctl load ~/Library/LaunchAgents/com.lizhun.openai-proxy.plist

# 查看状态
launchctl list | grep openai-proxy

# 重启服务
launchctl unload ~/Library/LaunchAgents/com.lizhun.openai-proxy.plist
launchctl load ~/Library/LaunchAgents/com.lizhun.openai-proxy.plist
```

## 工作原理

```
┌──────────────┐     OpenAI 格式      ┌───────────────┐     签名请求      ┌─────────────────┐
│  客户端/SDK   │ ──────────────────→ │  代理 (8088)   │ ──────────────→ │  super-agent     │
│  (curl/SDK)  │ ←────────────────── │  (Python)     │ ←────────────── │  (4397)          │
└──────────────┘    流式 SSE / JSON   └───────────────┘   SSE 事件流     └─────────────────┘
                                                                            │
                                                                    ┌───────┴───────┐
                                                                    │  TeleAgent     │
                                                                    │  桌面应用       │
                                                                    └───────────────┘
```

1. 客户端发送标准 OpenAI 格式请求到代理
2. 代理创建 super-agent 会话，启动 SSE 事件监听
3. 代理通过 `POST /session/{id}/prompt_async` 异步发送消息
4. 代理监听 `/global/event` SSE 事件流，收集 assistant 回复
5. 代理将回复转换为 OpenAI 格式返回（流式或非流式）

### 签名机制

代理自动从 scheduler 进程环境变量中提取 `SUPER_AGENT_LOCAL_SESSION_KEY`，使用 HMAC-SHA256 + base64url 生成签名头：

```
payload = "\n".join(["local-v1", method, path_with_query, timestamp_ms, nonce])
signature = HMAC-SHA256(session_key, payload).digest("base64url")
```

请求头：`X-SA-Sign-Version`, `X-SA-Timestamp`, `X-SA-Nonce`, `X-SA-Signature`

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/models` | 获取模型列表 (OpenAI 兼容) |
| POST | `/v1/chat/completions` | 聊天补全，支持 stream (OpenAI 兼容) |
| GET | `/health` | 健康检查 |
| GET | `/console` | Web 控制台页面 |
| GET | `/api/status` | 代理服务状态 |
| GET | `/api/models` | 详细模型列表 |
| GET | `/api/logs` | 请求日志 |
| GET | `/api/sessions` | super-agent 会话列表 |
| GET | `/api/stats` | 统计数据 |
| POST | `/api/test` | 在线测试接口 |

## 技术细节

- **零依赖**：纯 Python 标准库（`http.server`, `hmac`, `json`, `threading`），无第三方包
- **线程安全**：每个请求独立线程处理，SSE 监听器在子线程运行
- **会话隔离**：每次 API 请求创建独立的 super-agent 会话
- **日志环形缓冲**：保留最近 200 条请求日志
- **Token 统计**：从 SSE 事件中提取输入/输出/推理/缓存 token

## License

MIT
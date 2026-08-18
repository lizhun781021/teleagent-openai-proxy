---
name: openai-proxy-manager
description: 管理 TeleAgent OpenAI 兼容代理服务（8088端口），支持启动/停止/重启/状态查看/测试、默认模型查看与切换、模型列表查询。当用户提到代理服务管理、默认模型修改、模型切换、8088 端口服务、OpenAI 兼容代理、proxy 管理时使用。
name_cn: OpenAI 代理管理器
description_cn: 管理本地 OpenAI 兼容代理服务（8088），支持启停/状态/日志/测试，以及默认模型查看与切换、模型列表查询
---

# OpenAI Proxy Manager

管理 TeleAgent OpenAI 兼容代理服务的技能。该代理将 TeleAgent 的 AI 能力通过标准 OpenAI API 接口暴露出来，供企微机器人、外部应用等调用。

## 代理服务说明

- **监听地址**: `0.0.0.0:8088`
- **接口**: OpenAI 兼容 `/v1/chat/completions`、`/v1/models`
- **后端**: TeleAgent super-agent（自动获取 `SUPER_AGENT_LOCAL_SESSION_KEY` 签名）
- **launchd服务**: `com.lizhun.openai-proxy`（KeepAlive=true，开机自启）
- **脚本位置**: `~/Desktop/星小辰工作空间/openai-proxy/openai_proxy.py`
- **管理脚本**: `~/Desktop/星小辰工作空间/openai-proxy/openai_proxy_manager.py`
- **Web 控制台**: `http://127.0.0.1:8088/console`

## 服务管理命令

```bash
python3 ~/Desktop/星小辰工作空间/openai-proxy/openai_proxy_manager.py <command>
```

| 命令 | 说明 |
|------|------|
| `start` | 启动代理服务 |
| `stop` | 停止代理服务 |
| `restart` | 重启代理服务 |
| `status` | 查看服务运行状态 |
| `logs` | 查看最近日志（默认30行） |
| `test` | 发送测试请求验证代理是否正常 |
| `info` | 显示代理详细信息（地址、模型数、控制台链接等） |

## 默认模型管理

代理支持动态修改默认模型（影响所有未显式指定 `model` 字段的 API 请求），可直接调用本技能内置脚本：

```bash
python3 ~/.config/TeleAgent/skills/openai-proxy-manager/scripts/proxy_model.py <command>
```

| 命令 | 说明 |
|------|------|
| `status` | 查看当前默认模型与代理状态 |
| `set <provider/model>` | 设置默认模型，如 `NewApi/chat-pro`（立即生效） |
| `list` | 列出 TeleAgent 中已配置成功的模型（按 Provider 分组） |

### 模型列表说明

- 面板与 `list` 命令**只显示 TeleAgent 中已配置成功的 Provider**（`source=config`，即已在 TeleAgent 配置 API key/BaseURL 的），未配置密钥的内置模型目录（`source=custom`）不会显示
- 面板每 30 秒自动同步，TeleAgent 新增配置后最多 30 秒出现在面板
- OpenAI 兼容接口 `/v1/models` 不受过滤影响，仍返回全部模型（供外部工具使用）

### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/default-model` | 获取当前默认模型 |
| POST | `/api/default-model` | 修改默认模型（JSON: `{"model": "provider/model"}`） |
| GET | `/api/models` | 已配置模型列表（仅含 `source=config`） |

## 注意事项

- 代理依赖 TeleAgent 主进程运行，若 TeleAgent 未启动则代理无法工作
- 代理只支持文本对话，多模态 `image_url` 会被丢弃；图片需通过 prompt 中写绝对路径让 super-agent 用 `image_understanding` 工具查看
- 修改默认模型后立即生效，会影响所有未显式指定 `model` 字段的请求；如配置了多个 Provider，建议在外部工具中显式指定 `model` 避免依赖默认值
- 企微机器人通过此代理调用 TeleAgent 实现 AI 对话能力
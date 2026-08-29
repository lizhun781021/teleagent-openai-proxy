---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '94ca4f52-0fbb-40d0-93cf-0bdeec41d166'
  PropagateID: '94ca4f52-0fbb-40d0-93cf-0bdeec41d166'
  ReservedCode1: 'b6ae3d08-6e8f-4946-ae31-1c0e6aea7da6'
  ReservedCode2: 'b6ae3d08-6e8f-4946-ae31-1c0e6aea7da6'
---

# Changelog

本项目的所有重要变更记录在此文件中。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [1.8.0] - 2026-08-29

### 新增

- **Tool Call 转发支持（function calling 桥接）**
  - `send_prompt_async` 新增 `tools` 参数：收到 OpenAI 格式的 `tools` 列表后，自动将工具描述注入到发给 TeleAgent 的 system prompt 中
  - LLM 通过 `[TOOL_CALL]{"name": "...", "arguments": {...}}[/TOOL_CALL]` 标记输出工具调用
  - 新增 `_build_tool_prompt()` 和 `_parse_tool_calls_from_text()` 辅助函数
  - **流式模式**：智能缓冲检测——前 11 字符（`[TOOL_CALL]` 长度）缓冲判断，确认是 tool call 则缓冲完整响应后解析为 OpenAI `tool_calls` delta + `finish_reason: "tool_calls"`；普通对话立即流式输出不受影响
  - **非流式模式**：直接从响应文本解析 tool_calls，返回标准 OpenAI 格式
  - 支持 `role: "tool"` 消息（工具返回结果作为 `[tool_result]` 注入对话上下文）
  - 让 s2s / 机器人能通过标准 OpenAI function calling 协议调用 robot_vision 等工具

## [1.7.0] - 2026-08-29

### 新增

- **常驻全局确认监听器（多轮确认支持）**
  - 新增 `GlobalConfirmationListener` 后台线程：常驻连接 super-agent 的 `/global/event` 事件流，捕获**所有会话**的 `permission.asked` / `question.asked` 事件并登记到待确认表
  - 不依赖任何 chat/completions 请求的 SSE 连接——即使一次请求的连接在第一次确认后关闭，后续轮次确认（AI 确认后继续执行又触发新敏感操作）也能被捕获
  - 断线自动重连（5 秒间隔）
- **session_id → 会话标题关联映射**
  - 新增 `_session_title_map`：登记确认事件时自动补全会话标题，机器人侧轮询 `/api/permission/pending` 时可据此反查推送地址
  - 按会话标题前缀匹配（含 `|` 的标题按 `|` 前部分匹配，时间变化仍复用会话）
- **`POST /api/permission/inject` 测试接口（本地测试专用）**
  - 用于验证 8088 → 机器人轮询 → QQ 推送完整链路，不依赖 AI 是否弹确认
  - 仅允许本机（127.0.0.1 / ::1）调用，局域网/外网访问返回 403

### 修复

- **待确认登记幂等化（双通道去重）**
  - 同一 conf_id 可能被全局监听器与请求链路 SSE 双通道捕获，旧代码会重复登记、重复触发回调
  - 修复：`register_pending_confirmation` 已存在时仅补全缺失字段（title/session_id/description/tool/type），不刷新 time、不重复触发 `on_confirmation`，返回 `is_new=False` 供调用方判断
  - 避免机器人侧重复推送通知、重复 reply 导致 404

## [1.6.2] - 2026-08-29

### 修复

- **permission.asked 事件字段解析错误（根因修复）**
  - super-agent 的 `permission.asked` 事件结构为 `{id, sessionID, permission, patterns, always, tool}`
  - 旧代码用 `props.get("permissionID")` 取权限 ID，实际字段名是 `id` → 永远取到空值 → 不进入处理分支 → `on_confirmation` 回调不被调用
  - 旧代码用 `props.get("description")` 取描述，实际不存在此字段 → confirmation chunk 的 description 为空 → 机器人通知消息无内容
  - 旧代码用 `props.get("tool")` 取工具名，实际是 dict `{messageID, callID}` → 下游解析异常
  - 修复：
    - ID 字段：优先取 `props.get("id")`，兼容 `permissionID`/`permissionId`/`permission_id`
    - 描述：从 `permission` + `patterns` 字段自动拼装人类可读描述（如"权限类型: external_directory | 路径: /usr/local/*"）
    - 工具：tool 为 dict 时提取 `callID`，为字符串时直接使用
  - 同步修复 `question.asked` 事件的相同问题（ID 字段名 + tool 类型）

## [1.6.1] - 2026-08-29

### 修复

- **StreamingSSEListener 缺少权限确认事件处理（严重 Bug）**
  - `StreamingSSEListener._handle_event` 覆写父类时遗漏了 `permission.asked` 和 `question.asked` 事件分支
  - 导致流式请求中 AI 弹出权限确认/问题确认时，`on_confirmation` 回调永远不会被调用
  - 确认信号无法注入 SSE 流 → 机器人侧（企微/QQ/量子密信）收不到 confirmation chunk → 用户在聊天里永远看不到"请回复确认/拒绝"通知
  - 修复：在 `_handle_event` 中补上 `permission.asked` 和 `question.asked` 的完整处理逻辑（与父类 `SSEListener` 一致）

## [1.6.0] - 2026-08-19

### 新增

- 会话按标题复用：`POST /v1/chat/completions` 请求带 `session_title` 时，若 super-agent 已存在同名会话则复用其 session_id，不再每次新建
- 新增 `get_or_create_session()` / `get_session_by_title()`：按标题精确匹配 + 同目录过滤 + 取最新更新会话 + 本地 120 秒缓存 + 同名并发创建互斥
- 请求日志新增 `session_title` / `session_reused` 字段，便于排查会话复用情况

### 变更

- 会话标题语义调整：机器人端（企微/QQ）不再在标题中带时间戳，改为稳定标识（私聊=userid/openid，群聊=chatid/group_openid），同一用户/同一群固定一个会话，对话上下文可跨消息延续

## [1.5.1] - 2026-08-18

### 新增

- 项目内备份最新版「OpenAI 代理管理器」技能（`skills/openai-proxy-manager/`）：含 SKILL.md 与 `scripts/proxy_model.py`（默认模型 status/set/list 管理脚本）

## [1.5.0] - 2026-08-18

### 新增

- 新增动态默认模型管理：代理启动后可通过 Web 控制台修改默认模型，立即生效，影响所有未显式指定 `model` 字段的 API 请求
- 新增后端接口 `GET /api/default-model`（获取当前默认模型）与 `POST /api/default-model`（修改默认模型）
- Web 控制台「系统」页新增「默认模型设置」卡片：下拉选择模型、保存设置、刷新状态
- 模型列表自动同步：面板每 30 秒自动刷新模型列表，TeleAgent 新增 Provider/模型配置后最多 30 秒自动出现在面板；下拉框重建时保留用户当前选中项

### 变更

- `/api/models`（管理面板接口）只返回 TeleAgent 中已配置成功的 Provider（`source=config`），未配置密钥的内置模型目录（`source=custom`）不再显示；OpenAI 兼容接口 `/v1/models` 行为不变
- README 更新：模型说明由"224 个模型 / 19 个 Provider"改为"仅展示已配置的 Provider/模型"，补充默认模型设置与自动同步说明，新增 `/api/default-model` 端点文档

### 修复

- SSE 流式响应结束后主动关闭连接（`close_connection = True`），避免客户端 fetch 因等不到 EOF 而挂起

## 历史版本

以下为 git 历史提交中未单独记录 CHANGELOG 的版本演进（仓库初始化至 v1.5.0 之前）：

- 稳定性增强：`sa_request` 认证失败自动刷新 session key 重试；`create_session` 失败自动重试
- 支持 `session_title` 自定义会话标题；SSE 超时调整到 600 秒（适配企微机器人群聊调用场景）
- 使用 Homebrew Python 3.11 解决 launchd TCC 权限问题
- 新增 skill manager、launchd 配置，更新路径
- 初始版本：TeleAgent OpenAI-Compatible Proxy with web console
---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'af65fab9-0deb-423e-9228-aa96f676ba9d'
  PropagateID: 'af65fab9-0deb-423e-9228-aa96f676ba9d'
  ReservedCode1: '3e54bad2-f4c7-4cc6-84fc-5b9314bca07d'
  ReservedCode2: '3e54bad2-f4c7-4cc6-84fc-5b9314bca07d'
---

# Changelog

本项目的所有重要变更记录在此文件中。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

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
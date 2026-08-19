#!/usr/bin/env python3
"""
TeleAgent OpenAI-Compatible Proxy Server
=========================================
将本地 TeleAgent super-agent API 转换为 OpenAI 兼容接口。

支持端点:
  - GET  /v1/models           - 列出可用模型
  - POST /v1/chat/completions - 聊天补全（流式/非流式）

用法:
  python3 openai_proxy.py [--port 8088] [--host 0.0.0.0]

认证:
  调用时在 Authorization: Bearer <token> 中传入任意值即可（不做校验）。
  代理会自动从运行中的 scheduler 进程获取 super-agent 的 session key。
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import socket
import select
import uuid
import http.client
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# ============================================================
# 配置
# ============================================================
SUPER_AGENT_HOST = "127.0.0.1"
SUPER_AGENT_PORT = 4397
SUPER_AGENT_BASE = f"http://{SUPER_AGENT_HOST}:{SUPER_AGENT_PORT}"
SIGN_VERSION = "local-v1"
DEFAULT_DIRECTORY = os.path.expanduser("~/.local/share/TeleAgent/TeleAgent的工作空间")
DEFAULT_PROVIDER = "NewApi"
DEFAULT_MODEL = "chat-pro"

# 动态默认模型（可通过API修改）
_dynamic_default_provider = DEFAULT_PROVIDER
_dynamic_default_model = DEFAULT_MODEL

# 动态默认模型管理函数
def get_default_model():
    """获取当前默认模型"""
    with _default_model_lock:
        return f"{_dynamic_default_provider}/{_dynamic_default_model}"

def set_default_model(provider_id, model_id):
    """设置默认模型"""
    global _dynamic_default_provider, _dynamic_default_model
    with _default_model_lock:
        _dynamic_default_provider = provider_id
        _dynamic_default_model = model_id
    return True

def get_default_model_parts():
    """获取默认模型的provider和model部分"""
    with _default_model_lock:
        return _dynamic_default_provider, _dynamic_default_model

# Session key 缓存
_cached_session_key = None
_cached_session_key_time = 0
_session_key_lock = threading.Lock()
_default_model_lock = threading.Lock()  # 保护默认模型切换

# ============================================================
# 全局状态跟踪
# ============================================================
PROXY_START_TIME = time.time()

# 请求日志（环形缓冲，保留最近 200 条）
_request_log = []
_request_log_lock = threading.Lock()
_MAX_LOG = 200

# 统计数据
_stats_lock = threading.Lock()
_stats = {
    "total_requests": 0,
    "streaming_requests": 0,
    "non_streaming_requests": 0,
    "error_requests": 0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "total_tokens": 0,
    "model_usage": {},  # {"provider/model": count}
}


def log_request(entry):
    """记录一条请求日志"""
    with _request_log_lock:
        _request_log.append(entry)
        if len(_request_log) > _MAX_LOG:
            _request_log.pop(0)


def get_recent_logs(limit=50):
    """获取最近的请求日志"""
    with _request_log_lock:
        return list(reversed(_request_log[-limit:]))


def update_stats(model=None, streaming=False, error=False, tokens=None):
    """更新统计数据"""
    with _stats_lock:
        _stats["total_requests"] += 1
        if streaming:
            _stats["streaming_requests"] += 1
        else:
            _stats["non_streaming_requests"] += 1
        if error:
            _stats["error_requests"] += 1
        if model:
            _stats["model_usage"][model] = _stats["model_usage"].get(model, 0) + 1
        if tokens:
            _stats["total_input_tokens"] += tokens.get("input", 0)
            _stats["total_output_tokens"] += tokens.get("output", 0)
            _stats["total_tokens"] += tokens.get("total", 0)


def get_stats():
    """获取统计数据快照"""
    with _stats_lock:
        return dict(_stats)


def _format_uptime(seconds):
    """格式化运行时间"""
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if d > 0:
        parts.append(f"{d}天")
    if h > 0 or d > 0:
        parts.append(f"{h}小时")
    if m > 0 or h > 0 or d > 0:
        parts.append(f"{m}分")
    parts.append(f"{s}秒")
    return "".join(parts)


def _format_time(ts):
    """格式化时间戳"""
    t = time.localtime(ts)
    return time.strftime("%H:%M:%S", t)


def _format_ts_iso(ts):
    """ISO 格式时间"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def get_session_key():
    """从运行中的 scheduler 进程获取 SUPER_AGENT_LOCAL_SESSION_KEY"""
    global _cached_session_key, _cached_session_key_time

    with _session_key_lock:
        # 缓存 60 秒
        if _cached_session_key and time.time() - _cached_session_key_time < 60:
            return _cached_session_key

        # 尝试从环境变量获取
        key = os.environ.get("SUPER_AGENT_LOCAL_SESSION_KEY")
        if key:
            _cached_session_key = key
            _cached_session_key_time = time.time()
            return key

        # 从 scheduler/im-service 进程获取
        try:
            # 用 pgrep 找到进程 PID
            result = subprocess.run(
                ["pgrep", "-f", "scheduler/index.cjs"],
                capture_output=True, text=True, timeout=5
            )
            pids = result.stdout.strip().split("\n")
            for pid in pids:
                pid = pid.strip()
                if not pid:
                    continue
                try:
                    result2 = subprocess.run(
                        ["ps", "eww", pid], capture_output=True, text=True, timeout=5
                    )
                    match = re.search(r"SUPER_AGENT_LOCAL_SESSION_KEY=(\S+)", result2.stdout)
                    if match:
                        key = match.group(1)
                        _cached_session_key = key
                        _cached_session_key_time = time.time()
                        return key
                except:
                    pass
        except Exception as e:
            print(f"[WARN] 获取 session key 失败: {e}", file=sys.stderr)

        return _cached_session_key  # 返回可能过期的缓存


def sign_request(method, path_with_query):
    """生成 super-agent 认证签名头"""
    session_key = get_session_key()
    if not session_key:
        raise RuntimeError("无法获取 SUPER_AGENT_LOCAL_SESSION_KEY，请确保 TeleAgent 正在运行")

    timestamp = str(int(time.time() * 1000))
    nonce = uuid.uuid4().hex[:24]
    payload = "\n".join([SIGN_VERSION, method.upper(), path_with_query, timestamp, nonce])
    sig = hmac.new(session_key.encode(), payload.encode(), hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(sig).decode().rstrip("=")

    return {
        "X-SA-Sign-Version": SIGN_VERSION,
        "X-SA-Timestamp": timestamp,
        "X-SA-Nonce": nonce,
        "X-SA-Signature": signature,
    }


# ============================================================
# Super-Agent API 客户端
# ============================================================
def _force_refresh_session_key():
    """强制刷新 session key（清除缓存重新获取）"""
    global _cached_session_key, _cached_session_key_time
    with _session_key_lock:
        _cached_session_key = None
        _cached_session_key_time = 0
    return get_session_key()


def sa_request(method, path, body=None, timeout=30, _retry=True):
    """发送认证请求到 super-agent，认证失败自动刷新key重试一次"""
    headers = sign_request(method, path)
    url = f"{SUPER_AGENT_BASE}{path}"

    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode()
            return resp.status, resp_body, dict(resp.headers)
    except urllib.error.HTTPError as e:
        resp_body = e.read().decode()
        # 认证失败：强制刷新key重试一次
        if _retry and e.code in (401, 403):
            print(f"[WARN] super-agent认证失败({e.code})，刷新key重试...", file=sys.stderr)
            _force_refresh_session_key()
            return sa_request(method, path, body, timeout, _retry=False)
        return e.code, resp_body, dict(e.headers)
    except Exception as e:
        return None, str(e), {}


def create_session(directory=DEFAULT_DIRECTORY, title=None):
    """创建新的 super-agent 会话，失败时自动刷新key重试"""
    body = {"directory": directory} if directory else {}
    if title:
        body["title"] = title
    status, resp, _ = sa_request("POST", "/session", body)
    if status == 200:
        data = json.loads(resp)
        return data.get("id") or data.get("data", {}).get("id")
    # 重试一次（sa_request内部已处理认证重试，这里是额外保险）
    status, resp, _ = sa_request("POST", "/session", body, _retry=True)
    if status == 200:
        data = json.loads(resp)
        return data.get("id") or data.get("data", {}).get("id")
    return None


# ============================================================
# 会话按标题复用（企微/QQ机器人：同一用户/同一群复用同一会话）
# ============================================================
# 机器人端把会话标题固定为稳定标识（如 "企微|私聊|userid"、"QQ|群聊|group_openid"），
# 代理端按标题复用已存在会话，避免一句话开一个会话导致上下文断裂。
_session_cache = {}          # title -> (session_id, timestamp)
_session_cache_lock = threading.Lock()
_title_create_locks = {}     # title -> Lock（同名并发创建互斥）
SESSION_CACHE_TTL = 120      # 秒：本地缓存有效期（避免每次请求都查列表）
_SESSION_QUERY_LIMIT = 300   # 查询会话列表上限


def get_session_by_title(title, directory=DEFAULT_DIRECTORY):
    """在 super-agent 会话列表中按标题精确匹配，返回最新更新的 session_id（无则 None）
    只匹配同目录，避免误复用其他目录的同名会话。
    """
    if not title:
        return None
    status, resp, _ = sa_request("GET", f"/session?limit={_SESSION_QUERY_LIMIT}", timeout=10)
    if status != 200:
        return None
    try:
        sessions = json.loads(resp)
    except Exception:
        return None
    if not isinstance(sessions, list):
        return None
    matched = []
    for s in sessions:
        if s.get("title") != title:
            continue
        # 目录过滤：仅当两侧都有值时校验一致；super-agent 返回可能缺 directory 字段
        s_dir = s.get("directory", "") or ""
        if directory and s_dir and os.path.normpath(s_dir) != os.path.normpath(directory):
            continue
        matched.append(s)
    if not matched:
        return None
    # 取 updated 最新（time 可能缺失，防御处理）
    matched.sort(
        key=lambda s: ((s.get("time") or {}).get("updated") or 0),
        reverse=True
    )
    return matched[0].get("id")


def get_or_create_session(directory=DEFAULT_DIRECTORY, title=None):
    """按标题复用会话：标题已存在同名会话则返回其 session_id，否则新建。
    返回 (session_id, reused: bool)；失败返回 (None, False)。
    并发保护：同一标题同时请求时只创建一次。
    """
    if not title:
        return create_session(directory=directory, title=None), False

    now = time.time()
    # 1. 本地缓存命中
    with _session_cache_lock:
        cached = _session_cache.get(title)
        if cached and now - cached[1] < SESSION_CACHE_TTL:
            return cached[0], True

    # 2. 按标题查已有会话
    sid = get_session_by_title(title, directory=directory)
    if sid:
        with _session_cache_lock:
            _session_cache[title] = (sid, now)
        return sid, True

    # 3. 新建会话（进程内同名互斥，避免并发重复创建）
    with _session_cache_lock:
        _title_create_locks.setdefault(title, threading.Lock())
        create_lock = _title_create_locks[title]
    with create_lock:
        # 双检：等待锁期间可能已被其他线程创建
        _sid = get_session_by_title(title, directory=directory)
        if _sid:
            with _session_cache_lock:
                _session_cache[title] = (_sid, time.time())
            return _sid, True
        sid = create_session(directory=directory, title=title)
        if sid:
            with _session_cache_lock:
                _session_cache[title] = (sid, time.time())
        return sid, False


def send_prompt_async(session_id, messages, directory=DEFAULT_DIRECTORY,
                      provider_id=DEFAULT_PROVIDER, model_id=DEFAULT_MODEL):
    """异步发送消息到 super-agent 会话"""
    # 将 OpenAI messages 格式转换为 super-agent 的 prompt
    # super-agent 会话本身维护上下文，但我们这里每次创建新会话
    # 所以需要将历史消息合并为一个 prompt

    # 提取 system 消息
    system_content = None
    user_messages = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        # 处理 content 可能是 list 的情况（多模态）
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
            content = "\n".join(text_parts)
        elif not isinstance(content, str):
            content = str(content)

        if role == "system":
            if system_content:
                system_content += "\n" + content
            else:
                system_content = content
        elif role == "user":
            user_messages.append(content)
        elif role == "assistant":
            user_messages.append(f"[assistant] {content}")

    # 构建最终 prompt 文本
    # 如果只有一条 user 消息且无 system，直接用内容
    if len(user_messages) == 1 and not system_content:
        prompt_text = user_messages[0]
    else:
        # 多轮对话：格式化为对话历史
        parts = []
        if system_content:
            parts.append(system_content)
        for msg in user_messages:
            parts.append(msg)
        prompt_text = "\n\n".join(parts)

    body = {
        "sessionID": session_id,
        "parts": [{"type": "text", "text": prompt_text}],
    }
    if directory:
        body["directory"] = directory
    if provider_id and model_id:
        body["model"] = {"providerID": provider_id, "modelID": model_id}

    status, resp, _ = sa_request("POST", f"/session/{session_id}/prompt_async", body, timeout=10)
    return status == 204 or status == 200


def get_messages(session_id):
    """获取会话中的所有消息"""
    status, resp, _ = sa_request("GET", f"/session/{session_id}/message")
    if status == 200:
        return json.loads(resp)
    return []


def extract_assistant_text(messages):
    """从消息列表中提取最终的助手回复文本"""
    for msg in reversed(messages):
        info = msg.get("info", msg)
        role = info.get("role", "")
        if role == "assistant":
            # 检查是否有错误
            if info.get("error"):
                return None, info["error"]
            # 提取 text 类型的 parts
            parts = msg.get("parts", [])
            text_parts = []
            for part in parts:
                part_type = part.get("type", "")
                if part_type == "text" and part.get("text"):
                    text_parts.append(part["text"])
            if text_parts:
                return "\n".join(text_parts), None
    return None, None


# ============================================================
# SSE 事件流监听器
# ============================================================
class SSEListener:
    """监听 super-agent 的 /global/event SSE 流"""

    def __init__(self, session_id, timeout=120):
        self.session_id = session_id
        self.timeout = timeout
        self.sock = None
        self.buffer = ""
        self.header_parsed = False
        self.completed = False
        self.text_chunks = []  # 收到的文本增量
        self.reasoning_chunks = []  # 推理过程增量
        self.error = None
        self.tokens = None
        # 跟踪 assistant 消息的 message ID 和 text part ID
        self._assistant_msg_ids = set()
        self._assistant_text_part_ids = set()
        self._user_msg_ids = set()
        self._current_text_part_id = None

    def _make_request(self, path):
        headers = sign_request("GET", path)
        headers["Accept"] = "text/event-stream"
        headers["Host"] = f"{SUPER_AGENT_HOST}:{SUPER_AGENT_PORT}"

        req = f"GET {path} HTTP/1.1\r\n"
        for k, v in headers.items():
            req += f"{k}: {v}\r\n"
        req += "Connection: close\r\n\r\n"
        return req.encode()

    def listen(self):
        """监听 SSE 事件，直到收到完成或超时"""
        path = "/global/event"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((SUPER_AGENT_HOST, SUPER_AGENT_PORT))
        self.sock.sendall(self._make_request(path))

        start = time.time()
        current_text_part_id = None

        while time.time() - start < self.timeout:
            ready = select.select([self.sock], [], [], 1)
            if ready[0]:
                try:
                    chunk = self.sock.recv(8192)
                except:
                    break
                if not chunk:
                    break
                self.buffer += chunk.decode("utf-8", errors="replace")

                # 解析 HTTP 头
                if not self.header_parsed and "\r\n\r\n" in self.buffer:
                    _, self.buffer = self.buffer.split("\r\n\r\n", 1)
                    self.header_parsed = True

                if self.header_parsed:
                    # 处理 chunked encoding + SSE
                    # SSE 事件以 data: 行开头
                    lines = self.buffer.split("\n")
                    self.buffer = lines.pop()  # 保留最后一行（可能不完整）

                    for line in lines:
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        event_data = line[5:].strip()
                        if not event_data:
                            continue
                        # 跳过 hex chunk 大小行
                        try:
                            int(event_data, 16)
                            continue
                        except ValueError:
                            pass

                        try:
                            event = json.loads(event_data)
                        except json.JSONDecodeError:
                            continue

                        self._handle_event(event)

                        if self.completed:
                            break

            if self.completed:
                break

        try:
            self.sock.close()
        except:
            pass

    def _handle_event(self, event):
        """处理单个 SSE 事件"""
        payload = event.get("payload", {})
        etype = payload.get("type", "")
        props = payload.get("properties", {})

        # 只关注我们关心的 session 的事件
        session_id = props.get("sessionID", "")
        if session_id and session_id != self.session_id:
            return

        if etype == "message.updated":
            info = props.get("info", {})
            msg_role = info.get("role", "")
            msg_id = info.get("id", "")
            if msg_role == "assistant" and msg_id:
                self._assistant_msg_ids.add(msg_id)
            elif msg_role == "user" and msg_id:
                self._user_msg_ids.add(msg_id)
            if info.get("finish") == "error" or info.get("error"):
                self.error = info.get("error", {"message": "Unknown error"})

        elif etype == "message.part.updated":
            part = props.get("part", {})
            part_id = part.get("id", "")
            part_type = part.get("type", "")
            text = part.get("text", "")
            msg_id = part.get("messageID", props.get("messageID", ""))

            # 只跟踪 assistant 消息的 text parts
            if part_type == "text" and text and msg_id in self._assistant_msg_ids:
                self._assistant_text_part_ids.add(part_id)
                self._current_text_part_id = part_id
                # 如果没有收到 delta，用完整文本
                # 检查这个 part 的文本是否已经被 delta 收集了
                current_text = "".join(self.text_chunks)
                if text not in current_text:
                    self.text_chunks = [text]

            if part_type == "step-finish":
                tokens = part.get("tokens", {})
                if tokens and not self.tokens:
                    self.tokens = tokens

            if part.get("error"):
                self.error = part.get("error")

        elif etype == "message.part.delta":
            part_id = props.get("partID", "")
            field = props.get("field", "")
            delta = props.get("delta", "")

            if field == "text" and delta:
                # 只收集 assistant text part 的增量
                if part_id in self._assistant_text_part_ids:
                    self.text_chunks.append(delta)
                else:
                    self.reasoning_chunks.append(delta)

        elif etype == "session.status":
            status = props.get("status", {})
            if status.get("type") == "idle":
                self.completed = True

    def get_full_text(self):
        """获取完整的回复文本"""
        if self.text_chunks:
            return "".join(self.text_chunks)
        return ""


def listen_and_collect(session_id, timeout=120):
    """监听 SSE 并收集回复，返回 (text, reasoning, error, tokens)"""
    listener = SSEListener(session_id, timeout=timeout)
    listener.listen()
    return listener.get_full_text(), "".join(listener.reasoning_chunks), listener.error, listener.tokens


# ============================================================
# 流式 SSE 监听器（用于流式转发）
# ============================================================
class StreamingSSEListener(SSEListener):
    """监听 SSE 并实时回调文本增量"""

    def __init__(self, session_id, on_delta, on_complete, on_error, timeout=120):
        super().__init__(session_id, timeout)
        self.on_delta = on_delta
        self.on_complete = on_complete
        self.on_error = on_error
        self._text_started = False

    def _handle_event(self, event):
        payload = event.get("payload", {})
        etype = payload.get("type", "")
        props = payload.get("properties", {})

        session_id = props.get("sessionID", "")
        if session_id and session_id != self.session_id:
            return

        if etype == "message.updated":
            info = props.get("info", {})
            msg_role = info.get("role", "")
            msg_id = info.get("id", "")
            if msg_role == "assistant" and msg_id:
                self._assistant_msg_ids.add(msg_id)
            elif msg_role == "user" and msg_id:
                self._user_msg_ids.add(msg_id)
            if info.get("finish") == "error" or info.get("error"):
                self.error = info.get("error", {"message": "Unknown error"})
                self.on_error(self.error)

        elif etype == "message.part.updated":
            part = props.get("part", {})
            part_id = part.get("id", "")
            part_type = part.get("type", "")
            text = part.get("text", "")
            msg_id = part.get("messageID", props.get("messageID", ""))

            # 只处理 assistant 消息的 text parts
            if part_type == "text" and text and msg_id in self._assistant_msg_ids:
                self._assistant_text_part_ids.add(part_id)
                self._current_text_part_id = part_id
                # 发送完整文本块（如果还没开始流式输出）
                if not self._text_started:
                    self.text_chunks.append(text)
                    self.on_delta(text)
                    self._text_started = True

            if part_type == "step-finish":
                self.tokens = part.get("tokens", {})

            if part.get("error"):
                self.error = part.get("error")
                self.on_error(self.error)

        elif etype == "message.part.delta":
            part_id = props.get("partID", "")
            field = props.get("field", "")
            delta = props.get("delta", "")

            if field == "text" and delta:
                # 只转发 assistant text part 的增量
                if part_id in self._assistant_text_part_ids:
                    self._text_started = True
                    self.text_chunks.append(delta)
                    self.on_delta(delta)

        elif etype == "session.status":
            status = props.get("status", {})
            if status.get("type") == "idle":
                self.completed = True
                self.on_complete()


# ============================================================
# OpenAI 兼容 HTTP 服务器
# ============================================================
class OpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 简化日志
        print(f"[{self.log_date_time_string()}] {format % args}", file=sys.stderr)

    def _send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_headers(self):
        """发送 SSE 流式响应的 HTTP 头"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _send_sse_chunk(self, data):
        chunk = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.wfile.write(chunk.encode())
        self.wfile.flush()

    def _send_sse_end(self):
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        # SSE 流结束后必须关闭连接，否则客户端 fetch 永远等不到 EOF 而挂起
        self.close_connection = True

    def do_GET(self):
        # 去掉 query string
        path = self.path.split("?")[0]
        if path == "/v1/models":
            self._handle_models()
        elif path == "/health" or path == "/":
            self._send_json(200, {"status": "ok"})
        elif path == "/console" or path == "/dashboard":
            self._serve_console()
        elif path == "/api/status":
            self._handle_api_status()
        elif path == "/api/models":
            self._handle_api_models()
        elif path == "/api/logs":
            self._handle_api_logs()
        elif path == "/api/sessions":
            self._handle_api_sessions()
        elif path == "/api/stats":
            self._handle_api_stats()
        elif path == "/api/default-model":
            self._handle_api_default_model()
        else:
            self._send_json(404, {"error": {"message": "Not found", "type": "invalid_request"}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/v1/chat/completions":
            self._handle_chat_completions()
        elif path == "/api/test":
            self._handle_api_test()
        elif path == "/api/default-model":
            self._handle_api_default_model()
        else:
            self._send_json(404, {"error": {"message": "Not found", "type": "invalid_request"}})

    # ======== 管理 API ========

    def _handle_api_status(self):
        key = get_session_key()
        sa_status, sa_version = None, None
        try:
            status, resp, _ = sa_request("GET", "/global/health", timeout=5)
            if status == 200:
                d = json.loads(resp)
                sa_status = d.get("data", {}).get("status") or d.get("data", {}).get("healthy")
                sa_version = d.get("data", {}).get("version")
        except:
            pass
        sessions_list = []
        try:
            status, resp, _ = sa_request("GET", "/session?limit=10", timeout=5)
            if status == 200:
                sessions_list = json.loads(resp)
        except:
            pass
        uptime = int(time.time() - PROXY_START_TIME)
        self._send_json(200, {
            "proxy": {
                "status": "running",
                "uptime_seconds": uptime,
                "uptime_human": _format_uptime(uptime),
                "listen": f"0.0.0.0:{self.server.server_address[1]}",
                "default_model": get_default_model(),
                "default_directory": DEFAULT_DIRECTORY,
            },
            "super_agent": {
                "url": SUPER_AGENT_BASE,
                "status": sa_status or "unknown",
                "version": sa_version or "unknown",
                "session_key_cached": bool(key),
            },
            "stats": get_stats(),
            "recent_sessions": len(sessions_list),
        })

    def _handle_api_models(self):
        status, resp, _ = sa_request("GET", "/provider", timeout=10)
        providers = []
        if status == 200:
            try:
                data = json.loads(resp)
                for p in data.get("all", []):
                    # 只显示 TeleAgent 中已配置成功的 provider（source=config）
                    if p.get("source") != "config":
                        continue
                    pid = p.get("id", "")
                    pname = p.get("name", pid)
                    models = []
                    for mid, minfo in p.get("models", {}).items():
                        caps = minfo.get("capabilities", {})
                        models.append({
                            "id": mid, "full_id": f"{pid}/{mid}",
                            "name": minfo.get("name", mid), "capabilities": caps,
                        })
                    providers.append({
                        "id": pid, "name": pname, "source": p.get("source", ""),
                        "model_count": len(models), "models": models,
                    })
            except:
                pass
        self._send_json(200, {"providers": providers})

    def _handle_api_logs(self):
        limit = 100
        if "?" in self.path:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            limit = int(qs.get("limit", ["100"])[0])
        logs = get_recent_logs(limit)
        self._send_json(200, {"logs": logs, "total": len(logs)})

    def _handle_api_sessions(self):
        status, resp, _ = sa_request("GET", "/session?limit=20", timeout=10)
        sessions = []
        if status == 200:
            try:
                sessions = json.loads(resp)
            except:
                pass
        self._send_json(200, {"sessions": sessions})

    def _handle_api_stats(self):
        self._send_json(200, {
            "stats": get_stats(),
            "uptime_seconds": int(time.time() - PROXY_START_TIME),
        })

    def _handle_api_default_model(self):
        """处理默认模型设置请求"""
        # 获取当前默认模型
        if self.command == "GET":
            current = get_default_model()
            self._send_json(200, {"default_model": current})
            return
        
        # 设置新默认模型
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            req_data = json.loads(body)
        except:
            self._send_json(400, {"error": "Invalid JSON"})
            return
        
        model = req_data.get("model")
        if not model:
            self._send_json(400, {"error": "model is required"})
            return
        
        if "/" in model:
            provider_id, model_id = model.split("/", 1)
        else:
            # 如果没有指定provider，使用当前默认provider
            provider_id, model_id = get_default_model_parts()
            model_id = model
        
        if set_default_model(provider_id, model_id):
            self._send_json(200, {
                "success": True,
                "default_model": get_default_model(),
                "message": f"默认模型已更改为 {get_default_model()}"
            })
        else:
            self._send_json(500, {"error": "Failed to set default model"})

    def _handle_api_test(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            req_data = json.loads(body)
        except:
            self._send_json(400, {"error": "Invalid JSON"})
            return
        model = req_data.get("model", get_default_model())
        prompt = req_data.get("prompt", "你好")
        if "/" in model:
            provider_id, model_id = model.split("/", 1)
        else:
            provider_id, model_id = get_default_model_parts()
        session_id = create_session(directory=DEFAULT_DIRECTORY, title="console-test")
        if not session_id:
            self._send_json(500, {"error": "Failed to create session"})
            return
        start_ts = time.time()
        listener = StreamingSSEListener(
            session_id, on_delta=lambda t: None, on_complete=lambda: None,
            on_error=lambda e: None, timeout=120
        )
        sse_thread = threading.Thread(target=listener.listen)
        sse_thread.daemon = True
        sse_thread.start()
        time.sleep(0.5)
        send_prompt_async(session_id, [{"role": "user", "content": prompt}],
                          DEFAULT_DIRECTORY, provider_id, model_id)
        sse_thread.join(timeout=120)
        text = listener.get_full_text()
        duration = int((time.time() - start_ts) * 1000)
        self._send_json(200, {
            "model": model, "prompt": prompt, "response": text,
            "tokens": listener.tokens, "duration_ms": duration, "error": listener.error,
        })

    def _serve_console(self):
        html = CONSOLE_HTML
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ======== OpenAI 兼容 API ========

    def _handle_models(self):
        """列出可用模型"""
        # 从 super-agent 获取 provider 信息
        status, resp, _ = sa_request("GET", "/provider", timeout=10)
        models = []

        if status == 200:
            try:
                data = json.loads(resp)
                all_providers = data.get("all", [])
                for provider in all_providers:
                    pid = provider.get("id", "")
                    provider_models = provider.get("models", {})
                    for mid, minfo in provider_models.items():
                        models.append({
                            "id": f"{pid}/{mid}",
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": pid,
                        })
            except:
                pass

        if not models:
            # 返回默认模型
            models = [
                {"id": f"{DEFAULT_PROVIDER}/{DEFAULT_MODEL}", "object": "model",
                 "created": int(time.time()), "owned_by": DEFAULT_PROVIDER},
                {"id": f"{DEFAULT_PROVIDER}/chat-flash", "object": "model",
                 "created": int(time.time()), "owned_by": DEFAULT_PROVIDER},
            ]

        self._send_json(200, {"object": "list", "data": models})

    def _handle_chat_completions(self):
        """处理聊天补全请求"""
        # 读取请求体
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            req_data = json.loads(body)
        except Exception as e:
            self._send_json(400, {"error": {"message": f"Invalid JSON: {e}", "type": "invalid_request"}})
            return

        messages = req_data.get("messages", [])
        stream = req_data.get("stream", False)
        model = req_data.get("model", get_default_model())
        stream_options = req_data.get("stream_options", {}) or {}

        # 解析模型 provider/model
        if "/" in model:
            provider_id, model_id = model.split("/", 1)
        else:
            provider_id, model_id = get_default_model_parts()

        # 生成请求 ID
        request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if not messages:
            self._send_json(400, {"error": {"message": "messages is required", "type": "invalid_request"}})
            return

        # 读取自定义会话标题（机器人可传"姓名 | 技能 | 时间"）
        session_title = req_data.get("session_title") or f"API-{request_id}"

        # 创建会话（带标题时按标题复用，避免机器人一句话开一个会话）
        session_id, session_reused = get_or_create_session(
            directory=DEFAULT_DIRECTORY, title=session_title
        )
        if not session_id:
            self._send_json(500, {"error": {"message": "Failed to create session", "type": "server_error"}})
            return

        # 提取 prompt 摘要用于日志
        prompt_preview = ""
        if messages:
            last_user = None
            for m in reversed(messages):
                if m.get("role") == "user":
                    c = m.get("content", "")
                    if isinstance(c, list):
                        last_user = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
                    else:
                        last_user = str(c)
                    break
            prompt_preview = (last_user or "")[:100]

        # 记录请求开始
        log_entry = {
            "id": request_id,
            "timestamp": time.time(),
            "model": model,
            "stream": stream,
            "session_id": session_id,
            "session_title": session_title,
            "session_reused": session_reused,
            "status": "pending",
            "prompt_preview": prompt_preview,
            "response_preview": "",
            "duration_ms": 0,
            "tokens": None,
            "error": None,
        }
        log_request(log_entry)
        start_time = time.time()

        # 包装回调以记录日志
        if stream:
            self._handle_streaming_logged(session_id, messages, request_id, created,
                                          provider_id, model_id, log_entry, start_time,
                                          stream_options)
        else:
            self._handle_non_streaming_logged(session_id, messages, request_id, created,
                                               provider_id, model_id, log_entry, start_time)

    def _handle_streaming_logged(self, session_id, messages, request_id, created,
                                  provider_id, model_id, log_entry, start_time,
                                  stream_options=None):
        """流式响应（带日志）"""
        # 发送 SSE 响应头
        self._send_sse_headers()
        model_name = f"{provider_id}/{model_id}"
        first_chunk_sent = False
        collected_text = []
        collected_tokens = None
        has_error = False
        stream_options = stream_options or {}

        def send_delta(text):
            nonlocal first_chunk_sent
            if not first_chunk_sent:
                first_chunk = {
                    "id": request_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
                }
                self._send_sse_chunk(first_chunk)
                first_chunk_sent = True
            if text:
                collected_text.append(text)
                chunk = {
                    "id": request_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
                }
                self._send_sse_chunk(chunk)

        def send_complete():
            nonlocal collected_tokens
            if not first_chunk_sent:
                first_chunk = {
                    "id": request_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
                }
                self._send_sse_chunk(first_chunk)
            end_chunk = {
                "id": request_id, "object": "chat.completion.chunk",
                "created": created, "model": model_name,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            }
            self._send_sse_chunk(end_chunk)
            # 客户端请求 include_usage 时，发送带 usage 的最终 chunk（OpenAI/AI SDK 兼容）
            if stream_options.get("include_usage"):
                listener_tokens = getattr(listener, 'tokens', None) or {}
                usage = {
                    "prompt_tokens": listener_tokens.get("input", 0),
                    "completion_tokens": listener_tokens.get("output", 0),
                    "total_tokens": listener_tokens.get("total",
                                                       listener_tokens.get("input", 0) + listener_tokens.get("output", 0)),
                }
                usage_chunk = {
                    "id": request_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [],
                    "usage": usage,
                }
                self._send_sse_chunk(usage_chunk)
            self._send_sse_end()
            # 更新日志
            log_entry["status"] = "success"
            log_entry["response_preview"] = "".join(collected_text)[:100]
            log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
            listener_tokens = getattr(listener, 'tokens', None)
            if listener_tokens:
                log_entry["tokens"] = listener_tokens
            update_stats(model=model_name, streaming=True, tokens=listener_tokens)

        def send_error(err):
            nonlocal has_error
            has_error = True
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            error_chunk = {
                "id": request_id, "object": "chat.completion.chunk",
                "created": created, "model": model_name,
                "choices": [{"index": 0, "delta": {"content": f"[Error: {err_msg}]"}, "finish_reason": "stop"}]
            }
            self._send_sse_chunk(error_chunk)
            self._send_sse_end()
            log_entry["status"] = "error"
            log_entry["error"] = err_msg
            log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
            update_stats(model=model_name, streaming=True, error=True)

        listener = StreamingSSEListener(
            session_id, on_delta=send_delta, on_complete=send_complete,
            on_error=send_error, timeout=600
        )
        sse_thread = threading.Thread(target=listener.listen)
        sse_thread.daemon = True
        sse_thread.start()
        time.sleep(0.5)

        success = send_prompt_async(session_id, messages, DEFAULT_DIRECTORY,
                                     provider_id, model_id)
        if not success:
            send_error({"message": "Failed to send prompt to super-agent"})
            return

        sse_thread.join(timeout=600)
        if not listener.completed and not has_error:
            send_error({"message": "Response timeout"})

    def _handle_non_streaming_logged(self, session_id, messages, request_id, created,
                                      provider_id, model_id, log_entry, start_time):
        """非流式响应（带日志）"""
        model_name = f"{provider_id}/{model_id}"
        listener = StreamingSSEListener(
            session_id, on_delta=lambda t: None, on_complete=lambda: None,
            on_error=lambda e: None, timeout=600
        )
        sse_thread = threading.Thread(target=listener.listen)
        sse_thread.daemon = True
        sse_thread.start()
        time.sleep(0.5)

        success = send_prompt_async(session_id, messages, DEFAULT_DIRECTORY,
                                     provider_id, model_id)
        if not success:
            self._send_json(500, {"error": {"message": "Failed to send prompt", "type": "server_error"}})
            log_entry["status"] = "error"
            log_entry["error"] = "Failed to send prompt"
            log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
            update_stats(model=model_name, error=True)
            return

        sse_thread.join(timeout=600)
        text = listener.get_full_text()
        error = listener.error
        tokens = listener.tokens

        if error:
            err_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            self._send_json(500, {"error": {"message": err_msg, "type": "server_error"}})
            log_entry["status"] = "error"
            log_entry["error"] = err_msg
            log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
            update_stats(model=model_name, error=True)
            return

        if not text:
            msgs = get_messages(session_id)
            text, msg_error = extract_assistant_text(msgs)
            if msg_error:
                self._send_json(500, {"error": {"message": str(msg_error), "type": "server_error"}})
                log_entry["status"] = "error"
                log_entry["error"] = str(msg_error)
                log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
                update_stats(model=model_name, error=True)
                return
            if not text:
                self._send_json(500, {"error": {"message": "Empty response", "type": "server_error"}})
                log_entry["status"] = "error"
                log_entry["error"] = "Empty response"
                log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
                update_stats(model=model_name, error=True)
                return

        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if tokens:
            usage = {
                "prompt_tokens": tokens.get("input", 0),
                "completion_tokens": tokens.get("output", 0),
                "total_tokens": tokens.get("total", tokens.get("input", 0) + tokens.get("output", 0)),
            }
        response = {
            "id": request_id, "object": "chat.completion",
            "created": created, "model": model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage,
        }
        self._send_json(200, response)

        # 更新日志
        log_entry["status"] = "success"
        log_entry["response_preview"] = text[:100]
        log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
        log_entry["tokens"] = tokens
        update_stats(model=model_name, streaming=False, tokens=tokens)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ============================================================
# 控制台 HTML 页面
# ============================================================
CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TeleAgent 代理控制台</title>
<style>
:root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--text:#e4e4e7;--dim:#71717a;
--accent:#6366f1;--accent2:#818cf8;--green:#22c55e;--red:#ef4444;--yellow:#eab308;--blue:#3b82f6}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--text);font-size:14px;line-height:1.6}
a{color:var(--accent2);text-decoration:none}
/* 顶栏 */
.topbar{display:flex;align-items:center;justify-content:space-between;
padding:12px 24px;background:var(--card);border-bottom:1px solid var(--border);
position:sticky;top:0;z-index:100}
.topbar .logo{font-size:18px;font-weight:700;display:flex;align-items:center;gap:8px}
.topbar .logo .dot{width:8px;height:8px;border-radius:50%;background:var(--green)}
.topbar .logo .dot.offline{background:var(--red)}
.topbar nav{display:flex;gap:4px}
.topbar nav a{padding:6px 16px;border-radius:6px;color:var(--dim);font-size:13px;
transition:all .2s;cursor:pointer}
.topbar nav a:hover{color:var(--text);background:var(--border)}
.topbar nav a.active{color:var(--text);background:var(--accent)}
.topbar .meta{font-size:12px;color:var(--dim)}
/* 布局 */
.container{max-width:1400px;margin:0 auto;padding:24px}
.page{display:none}.page.active{display:block}
/* 卡片 */
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;
padding:20px;margin-bottom:16px}
.card-title{font-size:13px;font-weight:600;color:var(--dim);
text-transform:uppercase;letter-spacing:.5px;margin-bottom:16px;
display:flex;align-items:center;justify-content:space-between}
/* 统计卡片 */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}
.stat-item{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:16px}
.stat-item .label{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.stat-item .value{font-size:24px;font-weight:700;margin-top:4px}
.stat-item .sub{font-size:11px;color:var(--dim);margin-top:4px}
.stat-item.green .value{color:var(--green)}
.stat-item.red .value{color:var(--red)}
.stat-item.yellow .value{color:var(--yellow)}
.stat-item.blue .value{color:var(--blue)}
/* 状态指示器 */
.status-row{display:flex;align-items:center;gap:8px;padding:6px 0}
.status-row .key{color:var(--dim);min-width:140px;font-size:13px}
.status-row .val{font-family:monospace;font-size:13px}
.status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.status-dot.ok{background:var(--green)}.status-dot.err{background:var(--red)}
.status-dot.warn{background:var(--yellow)}
/* 表格 */
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 10px;color:var(--dim);font-weight:600;
border-bottom:1px solid var(--border);font-size:12px;text-transform:uppercase;letter-spacing:.3px}
td{padding:8px 10px;border-bottom:1px solid var(--border);max-width:300px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
tr:hover td{background:var(--bg)}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.tag.green{background:rgba(34,197,94,.15);color:var(--green)}
.tag.red{background:rgba(239,68,68,.15);color:var(--red)}
.tag.yellow{background:rgba(234,179,8,.15);color:var(--yellow)}
.tag.blue{background:rgba(59,130,246,.15);color:var(--blue)}
.tag.gray{background:rgba(113,113,122,.15);color:var(--dim)}
/* 模型列表 */
.model-group{margin-bottom:12px}
.model-group-header{display:flex;align-items:center;gap:8px;cursor:pointer;
padding:10px 12px;background:var(--bg);border-radius:8px;user-select:none}
.model-group-header:hover{background:var(--border)}
.model-group-header .arrow{transition:transform .2s;font-size:12px;color:var(--dim)}
.model-group-header .arrow.open{transform:rotate(90deg)}
.model-group-body{display:none;margin-top:4px}
.model-group-body.open{display:block}
.model-item{display:flex;align-items:center;justify-content:space-between;
padding:6px 12px 6px 32px;border-radius:6px;font-size:13px}
.model-item:hover{background:var(--bg)}
.model-item .id{font-family:monospace;color:var(--accent2)}
.model-item .caps{display:flex;gap:4px}
.cap{font-size:10px;padding:1px 6px;border-radius:3px;
background:var(--border);color:var(--dim)}
/* 测试 */
.test-area{display:flex;gap:16px}
.test-left{flex:1}
.test-right{flex:1}
.test-right .output{background:var(--bg);border:1px solid var(--border);
border-radius:8px;padding:16px;min-height:200px;white-space:pre-wrap;
font-family:monospace;font-size:13px;max-height:400px;overflow-y:auto}
textarea,select,input[type=text]{width:100%;background:var(--bg);
border:1px solid var(--border);border-radius:8px;padding:10px 12px;
color:var(--text);font-size:13px;font-family:inherit}
textarea:focus,select:focus,input:focus{outline:none;border-color:var(--accent)}
textarea{min-height:100px;resize:vertical}
label{font-size:12px;color:var(--dim);margin-bottom:4px;display:block}
button{padding:8px 20px;border-radius:8px;border:none;cursor:pointer;
font-size:13px;font-weight:600;transition:all .2s}
button.primary{background:var(--accent);color:#fff}
button.primary:hover{background:var(--accent2)}
button.primary:disabled{opacity:.5;cursor:not-allowed}
button.ghost{background:var(--border);color:var(--text)}
button.ghost:hover{background:var(--dim)}
/* 代码块 */
.code-block{background:var(--bg);border:1px solid var(--border);border-radius:8px;
  padding:14px 16px;font-family:monospace;font-size:12px;line-height:1.6;
  overflow-x:auto;white-space:pre;color:var(--text);margin:8px 0;position:relative}
.code-block .copy-btn{position:absolute;top:6px;right:6px;background:var(--border);
  border:none;color:var(--dim);font-size:11px;padding:3px 10px;border-radius:4px;
  cursor:pointer;transition:all .2s}
.code-block .copy-btn:hover{color:var(--text);background:var(--dim)}
.code-block .copy-btn.copied{color:var(--green)}
.guide-section{margin-bottom:28px}
.guide-section h3{font-size:15px;font-weight:600;margin-bottom:8px;color:var(--text);
  display:flex;align-items:center;gap:8px}
.guide-section h3 .num{display:inline-flex;align-items:center;justify-content:center;
  width:22px;height:22px;border-radius:50%;background:var(--accent);color:#fff;
  font-size:12px;font-weight:700}
.guide-section p{font-size:13px;line-height:1.7;color:var(--text);margin:6px 0}
.guide-section ul{margin:6px 0 6px 20px;font-size:13px;line-height:1.8;color:var(--text)}
.guide-section ul li{margin-bottom:2px}
.guide-section .tip{background:rgba(59,130,246,.08);border-left:3px solid var(--blue);
  padding:8px 14px;border-radius:0 8px 8px 0;font-size:12px;color:var(--dim);margin:8px 0}
.guide-section .warn{background:rgba(234,179,8,.08);border-left:3px solid var(--yellow);
  padding:8px 14px;border-radius:0 8px 8px 0;font-size:12px;color:var(--dim);margin:8px 0}
/* 日志 */
.log-filters{display:flex;gap:8px;margin-bottom:12px}
.scroll-table{max-height:600px;overflow-y:auto}
/* 工具栏 */
.toolbar{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.toolbar .spacer{flex:1}
/* 滚动条 */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--dim)}
</style>
</head>
<body>

<div class="topbar">
  <div class="logo">
    <span class="dot" id="statusDot"></span>
    TeleAgent 代理控制台
  </div>
  <nav>
    <a class="active" onclick="showPage('dashboard',this)">仪表盘</a>
    <a onclick="showPage('models',this)">模型</a>
    <a onclick="showPage('logs',this)">日志</a>
    <a onclick="showPage('test',this)">测试</a>
    <a onclick="showPage('guide',this)">说明</a>
    <a onclick="showPage('sessions',this)">会话</a>
    <a onclick="showPage('system',this)">系统</a>
  </nav>
  <div class="meta" id="topMeta">加载中...</div>
</div>

<div class="container">

<!-- ===== 仪表盘 ===== -->
<div class="page active" id="page-dashboard">
  <div class="stat-grid" id="statGrid"></div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px">
    <div class="card">
      <div class="card-title">服务状态</div>
      <div id="serviceStatus"></div>
    </div>
    <div class="card">
      <div class="card-title">模型调用统计</div>
      <div id="modelUsage"></div>
    </div>
  </div>
</div>

<!-- ===== 模型 ===== -->
<div class="page" id="page-models">
  <div class="card">
    <div class="card-title">
      可用模型列表
      <span id="modelCount" style="font-weight:400;color:var(--dim)"></span>
    </div>
    <div id="modelList"></div>
  </div>
</div>

<!-- ===== 日志 ===== -->
<div class="page" id="page-logs">
  <div class="card">
    <div class="card-title">
      请求日志
      <button class="ghost" onclick="loadLogs()">刷新</button>
    </div>
    <div class="scroll-table" id="logTable"></div>
  </div>
</div>

<!-- ===== 测试 ===== -->
<div class="page" id="page-test">
  <div class="card">
    <div class="card-title">在线测试</div>
    <div class="test-area">
      <div class="test-left">
        <div style="margin-bottom:12px">
          <label>模型</label>
          <select id="testModel"></select>
        </div>
        <div style="margin-bottom:12px">
          <label>Prompt</label>
          <textarea id="testPrompt" placeholder="输入测试消息...">1+1等于几？只回答数字</textarea>
        </div>
        <button class="primary" id="testBtn" onclick="runTest()">发送测试</button>
      </div>
      <div class="test-right">
        <label>响应结果</label>
        <div class="output" id="testOutput">等待测试...</div>
      </div>
    </div>
  </div>
</div>

<!-- ===== 会话 ===== -->
<div class="page" id="page-sessions">
  <div class="card">
    <div class="card-title">
      Super-Agent 会话列表
      <button class="ghost" onclick="loadSessions()">刷新</button>
    </div>
    <div class="scroll-table" id="sessionTable"></div>
  </div>
</div>

<!-- ===== 系统 ===== -->
<div class="page" id="page-system">
  <div class="card">
    <div class="card-title">系统信息</div>
    <div id="systemInfo"></div>
  </div>
  <div class="card">
    <div class="card-title">默认模型设置</div>
    <div style="margin-bottom:12px">
      <label>当前默认模型</label>
      <div style="display:flex;align-items:center;gap:12px;margin-top:8px">
        <select id="currentDefaultModel" style="flex:1"></select>
        <button class="primary" onclick="saveDefaultModel()">保存设置</button>
        <button class="ghost" onclick="loadDefaultModel()">刷新</button>
      </div>
      <div style="margin-top:8px;color:var(--dim);font-size:12px">
        修改后将立即生效，影响所有未指定模型的API请求
      </div>
    </div>
    <div id="defaultModelStatus" style="margin-top:8px"></div>
  </div>
  <div class="card">
    <div class="card-title">API 端点说明</div>
    <div id="apiDocs"></div>
  </div>
</div>

</div>

<!-- ===== 使用说明 ===== -->
<div class="page" id="page-guide">
  <div class="card" style="max-width:900px">

    <div class="guide-section">
      <h3><span class="num">1</span>服务概览</h3>
      <p>本代理将本地 TeleAgent 桌面应用的 super-agent API 转换为 <b>OpenAI 兼容接口</b>，
        任何支持 OpenAI API 的工具/SDK 均可直接接入。</p>
      <ul>
        <li><b>代理地址</b>：<code>http://127.0.0.1:8088</code>（局域网内其他设备可通过本机 IP 访问）</li>
        <li><b>认证方式</b>：无需认证，<code>Authorization: Bearer</code> 传任意值即可</li>
        <li><b>模型数量</b>：224 个（来自 19 个 Provider，含云端和本地模型）</li>
        <li><b>默认模型</b>：<code>NewApi/chat-pro</code></li>
        <li><b>后台运行</b>：通过 macOS launchd 持久化，开机自启、崩溃自动重启</li>
      </ul>
    </div>

    <div class="guide-section">
      <h3><span class="num">2</span>快速开始</h3>
      <p>最简单的调用方式 — 用 curl 发一个非流式请求：</p>
      <div class="code-block" id="curl-basic">curl http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any" \
  -d '{
    "model": "NewApi/chat-pro",
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'<button class="copy-btn" onclick="copyCode('curl-basic')">复制</button></div>
      <div class="tip">提示：<code>Authorization: Bearer</code> 可以省略，代理不校验 token。模型名格式为 <code>Provider/Model</code>。</div>
    </div>

    <div class="guide-section">
      <h3><span class="num">3</span>流式输出（SSE）</h3>
      <p>加 <code>"stream": true</code> 即可启用流式输出，返回标准 OpenAI SSE 格式（<code>data: {chunk}\n\n</code>）：</p>
      <div class="code-block" id="curl-stream">curl -N http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "NewApi/chat-pro",
    "messages": [{"role": "user", "content": "讲个笑话"}],
    "stream": true
  }'<button class="copy-btn" onclick="copyCode('curl-stream')">复制</button></div>
      <p>流式响应以 <code>data: [DONE]</code> 结束。每个 chunk 的 <code>delta.content</code> 包含增量文本。</p>
    </div>

    <div class="guide-section">
      <h3><span class="num">4</span>使用 Python OpenAI SDK</h3>
      <p>直接将 <code>base_url</code> 指向本代理即可，兼容 <code>openai</code> 官方 SDK：</p>
      <div class="code-block" id="py-sdk">from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8088/v1",
    api_key="any"  # 随意填，不校验
)

# 非流式
resp = client.chat.completions.create(
    model="NewApi/chat-pro",
    messages=[{"role": "user", "content": "你好"}]
)
print(resp.choices[0].message.content)

# 流式
stream = client.chat.completions.create(
    model="NewApi/chat-pro",
    messages=[{"role": "user", "content": "讲个笑话"}],
    stream=True
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")<button class="copy-btn" onclick="copyCode('py-sdk')">复制</button></div>
    </div>

    <div class="guide-section">
      <h3><span class="num">5</span>使用 requests / urllib（无需 SDK）</h3>
      <div class="code-block" id="py-requests">import requests

resp = requests.post("http://127.0.0.1:8088/v1/chat/completions", json={
    "model": "NewApi/chat-pro",
    "messages": [{"role": "user", "content": "你好"}]
})
print(resp.json()["choices"][0]["message"]["content"])<button class="copy-btn" onclick="copyCode('py-requests')">复制</button></div>
    </div>

    <div class="guide-section">
      <h3><span class="num">6</span>多轮对话</h3>
      <p>每次请求会自动创建一个新的 super-agent 会话。多轮对话只需在 <code>messages</code> 中传入完整历史：</p>
      <div class="code-block" id="curl-multi">curl http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "NewApi/chat-pro",
    "messages": [
      {"role": "user", "content": "我叫小明"},
      {"role": "assistant", "content": "你好小明！"},
      {"role": "user", "content": "我叫什么名字？"}
    ]
  }'<button class="copy-btn" onclick="copyCode('curl-multi')">复制</button></div>
      <div class="warn">注意：每次请求都是独立的会话，不会保留上一轮的上下文。多轮上下文需在 messages 数组中完整传入。</div>
    </div>

    <div class="guide-section">
      <h3><span class="num">7</span>获取模型列表</h3>
      <div class="code-block" id="curl-models">curl http://127.0.0.1:8088/v1/models<button class="copy-btn" onclick="copyCode('curl-models')">复制</button></div>
      <p>返回格式兼容 OpenAI <code>/v1/models</code>，模型 ID 格式为 <code>Provider/Model</code>。</p>
    </div>

    <div class="guide-section">
      <h3><span class="num">8</span>接入第三方工具</h3>
      <p>在支持自定义 OpenAI API 地址的工具中，填入以下配置：</p>
      <table style="margin:8px 0">
        <thead><tr><th style="width:140px">配置项</th><th>值</th></tr></thead>
        <tbody>
          <tr><td>API Base URL</td><td style="font-family:monospace">http://127.0.0.1:8088/v1</td></tr>
          <tr><td>API Key</td><td style="font-family:monospace">any（随意填写）</td></tr>
          <tr><td>模型名</td><td style="font-family:monospace">NewApi/chat-pro</td></tr>
        </tbody>
      </table>
      <div class="tip">已验证可接入：OpenAI Python SDK、LangChain、Cursor、Continue、ChatBox、NextChat 等。
        局域网内其他设备把 127.0.0.1 换成本机 IP 即可。</div>
    </div>

    <div class="guide-section">
      <h3><span class="num">9</span>服务管理</h3>
      <p>代理通过 macOS launchd 管理服务名 <code>com.lizhun.openai-proxy</code>：</p>
      <div class="code-block" id="svc-cmds"># 查看服务状态
launchctl list | grep openai-proxy

# 重启服务
launchctl unload ~/Library/LaunchAgents/com.lizhun.openai-proxy.plist
launchctl load ~/Library/LaunchAgents/com.lizhun.openai-proxy.plist

# 查看实时日志
tail -f /tmp/openai-proxy.log

# 手动前台运行（调试用）
python3 ~/Desktop/星小辰工作空间/openai-proxy/openai_proxy.py --port 8088 --host 0.0.0.0<button class="copy-btn" onclick="copyCode('svc-cmds')">复制</button></div>
    </div>

    <div class="guide-section">
      <h3><span class="num">10</span>注意事项</h3>
      <ul>
        <li><b>Session Key</b>：代理自动从运行中的 scheduler 进程获取签名密钥，TeleAgent 重启后自动刷新，无需手动配置</li>
        <li><b>请求延迟</b>：首次请求需创建会话 + 等待 SSE 事件，通常 3-6 秒；后续请求类似</li>
        <li><b>会话清理</b>：每次 API 请求会创建一个 super-agent 会话，可在「会话」页面查看</li>
        <li><b>并发限制</b>：底层 super-agent 为单实例，建议避免高并发请求</li>
        <li><b>Token 统计</b>：从 super-agent SSE 事件中提取，包含输入/输出/推理/缓存 token</li>
        <li><b>系统代理冲突</b>：若本机开了 HTTP 代理（如 Clash），curl 需加 <code>--noproxy '*'</code> 或设置 <code>no_proxy=127.0.0.1</code></li>
      </ul>
    </div>

  </div>
</div>

</div>

<script>
const API = '';
let modelCache = [];

// ===== 复制代码 =====
function copyCode(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const text = el.textContent.replace(/复制$/, '').trim();
  navigator.clipboard.writeText(text).then(() => {
    const btn = el.querySelector('.copy-btn');
    if (btn) { btn.textContent = '已复制'; btn.classList.add('copied');
      setTimeout(() => { btn.textContent = '复制'; btn.classList.remove('copied'); }, 1500); }
  });
}

// ===== 页面切换 =====
function showPage(name, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  document.querySelectorAll('.topbar nav a').forEach(a => a.classList.remove('active'));
  el.classList.add('active');
  // 切换页面时自动加载数据
  if (name === 'logs') loadLogs();
  if (name === 'sessions') loadSessions();
}

// ===== 状态加载 =====
async function loadStatus() {
  try {
    const r = await fetch(API + '/api/status');
    const d = await r.json();
    const p = d.proxy, sa = d.super_agent, s = d.stats;

    // 顶栏
    const dot = document.getElementById('statusDot');
    dot.className = 'dot' + (sa.status === 'ok' || sa.status === true ? '' : ' offline');
    document.getElementById('topMeta').textContent =
      `运行 ${p.uptime_human} · ${s.total_requests} 请求`;

    // 统计卡片
    const errRate = s.total_requests > 0
      ? ((s.error_requests / s.total_requests) * 100).toFixed(1) + '%' : '0%';
    document.getElementById('statGrid').innerHTML = [
      stat('总请求数', s.total_requests, '', 'blue'),
      stat('流式请求', s.streaming_requests, '非流式 ' + s.non_streaming_requests, ''),
      stat('错误请求', s.error_requests, '错误率 ' + errRate, s.error_requests > 0 ? 'red' : ''),
      stat('总 Token', formatNum(s.total_tokens), '输入 ' + formatNum(s.total_input_tokens)
        + ' · 输出 ' + formatNum(s.total_output_tokens), 'green'),
      stat('运行时间', p.uptime_human, '启动于 ' + new Date(Date.now() - p.uptime_seconds * 1000).toLocaleTimeString(), 'yellow'),
      stat('可用模型', '—', '点击模型页查看', ''),
    ].join('');

    // 服务状态
    document.getElementById('serviceStatus').innerHTML = `
      <div class="status-row"><span class="key">代理状态</span>
        <span class="val"><span class="status-dot ok"></span>运行中</span></div>
      <div class="status-row"><span class="key">监听地址</span><span class="val">${p.listen}</span></div>
      <div class="status-row"><span class="key">默认模型</span><span class="val">${p.default_model}</span></div>
      <div class="status-row"><span class="key">Super-Agent</span>
        <span class="val"><span class="status-dot ${sa.status === 'ok' || sa.status === true ? 'ok' : 'err'}"></span>
        ${sa.url} (v${sa.version})</span></div>
      <div class="status-row"><span class="key">Session Key</span>
        <span class="val"><span class="status-dot ${sa.session_key_cached ? 'ok' : 'err'}"></span>
        ${sa.session_key_cached ? '已缓存' : '未获取'}</span></div>
      <div class="status-row"><span class="key">工作目录</span><span class="val">${p.default_directory}</span></div>`;

    // 模型使用
    const mu = s.model_usage || {};
    const muEntries = Object.entries(mu).sort((a, b) => b[1] - a[1]);
    document.getElementById('modelUsage').innerHTML = muEntries.length === 0
      ? '<div style="color:var(--dim);text-align:center;padding:20px">暂无调用记录</div>'
      : '<table><thead><tr><th>模型</th><th style="width:80px">调用次数</th></tr></thead><tbody>'
        + muEntries.map(([m, c]) =>
          `<tr><td style="font-family:monospace">${esc(m)}</td><td>${c}</td></tr>`).join('')
        + '</tbody></table>';

    // 系统信息
    document.getElementById('systemInfo').innerHTML = `
      <div class="status-row"><span class="key">代理版本</span><span class="val">v1.0</span></div>
      <div class="status-row"><span class="key">Python</span><span class="val">${'Python 3'}</span></div>
      <div class="status-row"><span class="key">运行平台</span><span class="val">macOS (launchd)</span></div>
      <div class="status-row"><span class="key">super-agent 版本</span><span class="val">${sa.version || '未知'}</span></div>
      <div class="status-row"><span class="key">签名版本</span><span class="val">local-v1 (HMAC-SHA256 + base64url)</span></div>
      <div class="status-row"><span class="key">Session Key 来源</span><span class="val">pgrep scheduler → ps eww</span></div>
      <div class="status-row"><span class="key">SSE 事件流</span><span class="val">/global/event</span></div>
      <div class="status-row"><span class="key">会话创建方式</span><span class="val">每次请求新建会话</span></div>`;

    // API 文档
    document.getElementById('apiDocs').innerHTML = `
      <table><thead><tr><th>方法</th><th>路径</th><th>说明</th></tr></thead><tbody>
      <tr><td><span class="tag blue">GET</span></td><td style="font-family:monospace">/v1/models</td><td>获取模型列表 (OpenAI 兼容)</td></tr>
      <tr><td><span class="tag green">POST</span></td><td style="font-family:monospace">/v1/chat/completions</td><td>聊天补全，支持 stream (OpenAI 兼容)</td></tr>
      <tr><td><span class="tag blue">GET</span></td><td style="font-family:monospace">/health</td><td>健康检查</td></tr>
      <tr><td><span class="tag blue">GET</span></td><td style="font-family:monospace">/console</span></td><td>本控制台页面</td></tr>
      <tr><td><span class="tag blue">GET</span></td><td style="font-family:monospace">/api/status</td><td>代理服务状态</td></tr>
      <tr><td><span class="tag blue">GET</span></td><td style="font-family:monospace">/api/models</td><td>详细模型列表</td></tr>
      <tr><td><span class="tag blue">GET</span></td><td style="font-family:monospace">/api/logs</td><td>请求日志</td></tr>
      <tr><td><span class="tag blue">GET</span></td><td style="font-family:monospace">/api/sessions</td><td>Super-Agent 会话列表</td></tr>
      <tr><td><span class="tag blue">GET</span></td><td style="font-family:monospace">/api/stats</td><td>统计数据</td></tr>
      <tr><td><span class="tag green">POST</span></td><td style="font-family:monospace">/api/test</td><td>在线测试接口</td></tr>
      </tbody></table>`;

  } catch(e) {
    document.getElementById('statusDot').className = 'dot offline';
    document.getElementById('topMeta').textContent = '连接失败';
  }
}

// ===== 默认模型管理 =====
async function loadDefaultModel() {
  try {
    const r = await fetch(API + '/api/default-model');
    const d = await r.json();
    const currentModel = d.default_model;
    
    // 填充模型选择下拉框
    const sel = document.getElementById('currentDefaultModel');
    if (sel.options.length === 0) {
      // 从modelCache中加载模型选项
      modelCache.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m;
        opt.textContent = m;
        sel.appendChild(opt);
      });
    }
    
    // 设置当前默认模型
    for (let i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === currentModel) {
        sel.selectedIndex = i;
        break;
      }
    }
    
    document.getElementById('defaultModelStatus').innerHTML = 
      `<div style="color:var(--green);font-size:12px">当前默认模型: ${currentModel}</div>`;
  } catch(e) {
    document.getElementById('defaultModelStatus').innerHTML = 
      `<div style="color:var(--red);font-size:12px">加载失败: ${e.message}</div>`;
  }
}

async function saveDefaultModel() {
  const sel = document.getElementById('currentDefaultModel');
  const model = sel.value;
  if (!model) return;
  
  try {
    const r = await fetch(API + '/api/default-model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model })
    });
    const d = await r.json();
    
    if (d.success) {
      document.getElementById('defaultModelStatus').innerHTML = 
        `<div style="color:var(--green);font-size:12px">✓ ${d.message}</div>`;
      // 刷新仪表盘显示
      loadStatus();
    } else {
      document.getElementById('defaultModelStatus').innerHTML = 
        `<div style="color:var(--red);font-size:12px">保存失败: ${d.error}</div>`;
    }
  } catch(e) {
    document.getElementById('defaultModelStatus').innerHTML = 
      `<div style="color:var(--red);font-size:12px">请求失败: ${e.message}</div>`;
  }
}

// ===== 模型 =====
async function loadModels() {
  try {
    const r = await fetch(API + '/api/models');
    const d = await r.json();
    modelCache = [];
    let total = 0;
    let html = '';
    for (const p of (d.providers || [])) {
      const id = 'mg-' + p.id.replace(/[^a-zA-Z0-9]/g, '');
      html += `<div class="model-group">
        <div class="model-group-header" onclick="toggleGroup('${id}')">
          <span class="arrow" id="arrow-${id}">▶</span>
          <span style="font-weight:600">${esc(p.name)}</span>
          <span class="tag gray">${p.id}</span>
          <span class="tag blue">${p.model_count} 模型</span>
          <span style="color:var(--dim);font-size:11px">来源: ${p.source}</span>
        </div>
        <div class="model-group-body" id="${id}">
          ${p.models.map(m => {
            modelCache.push(m.full_id);
            total++;
            const caps = [];
            if (m.capabilities?.reasoning) caps.push('推理');
            if (m.capabilities?.attachment) caps.push('附件');
            if (m.capabilities?.temperature) caps.push('温控');
            return `<div class="model-item">
              <span class="id">${esc(m.full_id)}</span>
              <span style="color:var(--dim)">${esc(m.name)}</span>
              <span class="caps">${caps.map(c => `<span class="cap">${c}</span>`).join('')}</span>
            </div>`;
          }).join('')}
        </div>
      </div>`;
    }
    document.getElementById('modelList').innerHTML = html;
    document.getElementById('modelCount').textContent = `共 ${d.providers?.length || 0} 个 Provider，${total} 个模型`;

    // 同步刷新测试页与系统页的模型下拉框（始终重建，保留当前选中项）
    refreshModelSelect('testModel', modelCache, 'NewApi/chat-pro');
    refreshModelSelect('currentDefaultModel', modelCache, null);
    // 同步默认模型显示状态
    loadDefaultModel();
  } catch(e) {
    document.getElementById('modelList').innerHTML = '<div style="color:var(--red)">加载失败</div>';
  }
}

// 重建模型下拉框：保留原选中项，若原选中项已不存在则保留第一个
function refreshModelSelect(selId, models, fallback) {
  const sel = document.getElementById(selId);
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '';
  models.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m; opt.textContent = m;
    sel.appendChild(opt);
  });
  if (prev && models.includes(prev)) {
    sel.value = prev;
  } else if (fallback && models.includes(fallback)) {
    sel.value = fallback;
  }
}
function toggleGroup(id) {
  const body = document.getElementById(id);
  const arrow = document.getElementById('arrow-' + id);
  body.classList.toggle('open');
  arrow.classList.toggle('open');
}

// ===== 日志 =====
async function loadLogs() {
  try {
    const r = await fetch(API + '/api/logs?limit=100');
    const d = await r.json();
    const logs = d.logs || [];
    if (logs.length === 0) {
      document.getElementById('logTable').innerHTML =
        '<div style="color:var(--dim);text-align:center;padding:20px">暂无请求日志</div>';
      return;
    }
    let html = '<table><thead><tr><th>时间</th><th>模型</th><th>类型</th><th>状态</th>'
      + '<th>Prompt</th><th>响应</th><th>耗时</th><th>Token</th></tr></thead><tbody>';
    for (const l of logs) {
      const time = new Date(l.timestamp * 1000).toLocaleTimeString();
      const tagClass = l.status === 'success' ? 'green' : l.status === 'error' ? 'red' : 'yellow';
      const streamTag = l.stream ? '<span class="tag blue">流式</span>' : '<span class="tag gray">非流式</span>';
      const tokens = l.tokens
        ? `${(l.tokens.input||0)+'+'}${(l.tokens.output||0)}=${(l.tokens.total||0)}`
        : '—';
      html += `<tr>
        <td style="white-space:nowrap">${time}</td>
        <td style="font-family:monospace;font-size:12px">${esc(l.model||'')}</td>
        <td>${streamTag}</td>
        <td><span class="tag ${tagClass}">${l.status}</span></td>
        <td title="${esc(l.prompt_preview||'')}">${esc(l.prompt_preview||'')}</td>
        <td title="${esc(l.response_preview||'')}">${esc(l.response_preview||'')}</td>
        <td>${l.duration_ms ? l.duration_ms + 'ms' : '—'}</td>
        <td style="font-size:12px">${tokens}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    document.getElementById('logTable').innerHTML = html;
  } catch(e) {
    document.getElementById('logTable').innerHTML = '<div style="color:var(--red)">加载失败</div>';
  }
}

// ===== 测试 =====
async function runTest() {
  const btn = document.getElementById('testBtn');
  const out = document.getElementById('testOutput');
  const model = document.getElementById('testModel').value;
  const prompt = document.getElementById('testPrompt').value;
  if (!prompt.trim()) return;
  btn.disabled = true;
  out.textContent = '发送中...';
  try {
    const r = await fetch(API + '/api/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, prompt })
    });
    const d = await r.json();
    if (d.error) {
      out.innerHTML = `<span style="color:var(--red)">错误: ${esc(d.error.message || d.error)}</span>`;
    } else {
      const t = d.tokens || {};
      out.innerHTML = `${esc(d.response || '(空响应)')}\n\n` +
        `<span style="color:var(--dim)">---\n` +
        `模型: ${esc(d.model)}\n` +
        `耗时: ${d.duration_ms}ms\n` +
        `Token: 输入 ${t.input||0} + 输出 ${t.output||0} = ${t.total||0}</span>`;
    }
  } catch(e) {
    out.innerHTML = `<span style="color:var(--red)">请求失败: ${esc(e)}</span>`;
  }
  btn.disabled = false;
}

// ===== 会话 =====
async function loadSessions() {
  try {
    const r = await fetch(API + '/api/sessions');
    const d = await r.json();
    const ss = d.sessions || [];
    if (ss.length === 0) {
      document.getElementById('sessionTable').innerHTML =
        '<div style="color:var(--dim);text-align:center;padding:20px">暂无会话</div>';
      return;
    }
    let html = '<table><thead><tr><th>标题</th><th>Session ID</th><th>目录</th>'
      + '<th>创建时间</th><th>更新时间</th></tr></thead><tbody>';
    for (const s of ss) {
      const created = s.time?.created ? new Date(s.time.created).toLocaleString() : '—';
      const updated = s.time?.updated ? new Date(s.time.updated).toLocaleString() : '—';
      html += `<tr>
        <td>${esc(s.title||'(无标题)')}</td>
        <td style="font-family:monospace;font-size:12px">${esc(s.id||'')}</td>
        <td style="font-size:12px" title="${esc(s.directory||'')}">${esc(s.directory||'')}</td>
        <td style="white-space:nowrap">${created}</td>
        <td style="white-space:nowrap">${updated}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    document.getElementById('sessionTable').innerHTML = html;
  } catch(e) {
    document.getElementById('sessionTable').innerHTML = '<div style="color:var(--red)">加载失败</div>';
  }
}

// ===== 工具函数 =====
function stat(label, value, sub, cls) {
  return `<div class="stat-item ${cls||''}">
    <div class="label">${label}</div>
    <div class="value">${value}</div>
    ${sub ? `<div class="sub">${sub}</div>` : ''}
  </div>`;
}
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}
function formatNum(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

// ===== 初始化 =====
loadStatus();
loadModels();
// 延迟加载默认模型，确保modelCache已填充
setTimeout(loadDefaultModel, 1000);
// 每 30 秒自动刷新模型列表，TeleAgent 新增配置后自动同步到面板
setInterval(loadModels, 30000);
setInterval(loadStatus, 5000);
</script>
</body>
</html>
"""


# ============================================================
# 主入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="TeleAgent OpenAI-Compatible Proxy")
    parser.add_argument("--port", type=int, default=8088, help="监听端口 (默认: 8088)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认: 0.0.0.0)")
    args = parser.parse_args()

    # 验证可以获取 session key
    key = get_session_key()
    if not key:
        print("[ERROR] 无法获取 SUPER_AGENT_LOCAL_SESSION_KEY", file=sys.stderr)
        print("请确保 TeleAgent 正在运行", file=sys.stderr)
        sys.exit(1)

    # 测试 super-agent 连接
    status, _, _ = sa_request("GET", "/global/health", timeout=5)
    if status != 200:
        print(f"[ERROR] super-agent 健康检查失败 (status={status})", file=sys.stderr)
        sys.exit(1)

    # 获取可用模型列表
    status, resp, _ = sa_request("GET", "/provider", timeout=10)
    model_count = 0
    if status == 200:
        try:
            data = json.loads(resp)
            for p in data.get("all", []):
                model_count += len(p.get("models", {}))
        except:
            pass

    print(f"""
╔══════════════════════════════════════════════════════════╗
║     TeleAgent OpenAI-Compatible Proxy Server             ║
╠══════════════════════════════════════════════════════════╣
  监听地址: {args.host}:{args.port}
  super-agent: {SUPER_AGENT_BASE}
  可用模型数: {model_count}
  默认模型: {DEFAULT_PROVIDER}/{DEFAULT_MODEL}

  端点:
    GET  /v1/models          - 列出模型
    POST /v1/chat/completions - 聊天补全 (支持 stream)
    GET  /health             - 健康检查

  使用示例:
    curl http://{args.host}:{args.port}/v1/chat/completions \\
      -H "Content-Type: application/json" \\
      -H "Authorization: Bearer any" \\
      -d '{{"model":"{DEFAULT_PROVIDER}/{DEFAULT_MODEL}","messages":[{{"role":"user","content":"你好"}}]}}'
╚══════════════════════════════════════════════════════════╝
    """)

    server = ThreadingHTTPServer((args.host, args.port), OpenAIHandler)
    print(f"[*] 服务器已启动，等待请求...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 正在关闭...")
        server.shutdown()


if __name__ == "__main__":
    main()

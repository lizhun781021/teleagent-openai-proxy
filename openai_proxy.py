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

# 待确认请求注册表（permissionID/questionID -> 描述信息）
# 供机器人侧通过 /api/permission/reply 接口完成确认/拒绝
_pending_confirmations = {}      # id -> {type, session_id, title, description, tool, time}
_pending_confirmations_lock = threading.Lock()
_PENDING_CONFIRM_TTL = 1800      # 30分钟未处理自动清理

# session_id -> session_title 映射（供确认事件关联到机器人侧会话标题）
_session_title_map = {}          # session_id -> title
_session_title_map_lock = threading.Lock()

# 请求日志（环形缓冲，保留最近 200 条）
_request_log = []
_request_log_lock = threading.Lock()
_MAX_LOG = 200

# ============================================================
# 请求日志落盘（JSON Lines，按天滚动，重启不丢）
# ============================================================
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_disk_log_lock = threading.Lock()


def _log_file_path(ts=None):
    """按天滚动日志文件名: logs/requests-YYYY-MM-DD.jsonl"""
    day = time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))
    return os.path.join(LOG_DIR, f"requests-{day}.jsonl")


def log_request_to_disk(entry):
    """将一条请求日志以 JSON Lines 追加写盘。

    同一条请求（按 id 去重）会有两次写入：请求开始时 status=pending，
    结束时 status=success/error。为避免重复，落盘采用"同 id 覆盖"：
    每次追加前先扫描当日文件，若已有同 id 记录则就地替换。
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = _log_file_path()
        req_id = entry.get("id", "")
        with _disk_log_lock:
            # 读当日已有记录，去掉同 id 的旧记录
            lines = []
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            kept = [ln for ln in lines if (req_id and f'"id": "{req_id}"' not in ln)]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(kept)
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"[LOG-DISK] 落盘失败: {e}", file=sys.stderr)


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
    """记录一条请求日志（内存环形缓冲 + 落盘持久化）"""
    with _request_log_lock:
        _request_log.append(entry)
        if len(_request_log) > _MAX_LOG:
            _request_log.pop(0)
    log_request_to_disk(entry)


# ============================================================
# 调用进程反查（lsof → PID → 友好名称）
# ============================================================
_caller_name_cache = {}   # (ip, port) -> {name, pid, cmd, time}
_caller_name_lock = threading.Lock()
_CALLER_CACHE_TTL = 120   # 2分钟缓存


def _friendly_caller_name(pid, cmd, cwd):
    """把进程命令行映射为友好名称"""
    cmd_lower = (cmd or "").lower()

    # TeleAgent 主程序（Electron 应用内嵌的 Node 进程）
    if "teleagent" in cmd_lower or "super-agent" in cmd_lower or "opencowork" in cmd_lower:
        return "TeleAgent主程序"
    # wecom-bot（企微/QQ/密信机器人）
    if "wecom-bot" in cmd_lower or "server.py" in cmd_lower and "wecom" in (cwd or "").lower():
        return "wecom-bot"
    if "qq_official" in cmd_lower or "qq_adapter" in cmd_lower:
        return "QQ适配器"
    if "zmx_adapter" in cmd_lower:
        return "量子密信适配器"
    # 本地 AI 工厂
    if "local-ai-factory" in (cwd or "").lower() or "ai_factory" in cmd_lower or "streamlit" in cmd_lower and "ai" in (cwd or "").lower():
        return "AI工厂"
    # Reachy Mini 机器人
    if "reachy" in cmd_lower or "s2s" in cmd_lower or "speech-to-speech" in cmd_lower or "paraformer" in cmd_lower:
        return "Reachy Mini"
    # 机器人视觉（mlx_vlm 直调）
    if "mlx_vlm" in cmd_lower or "vision" in cmd_lower and "robot" in (cwd or "").lower():
        return "机器人视觉"
    # 控制台在线测试
    if "console-test" in cmd_lower:
        return "面板测试"
    # 通用工具
    if cmd_lower.startswith("curl"):
        return "curl"
    if "python" in cmd_lower and "openai_proxy" in cmd_lower:
        return "8088代理自身"
    if "node" in cmd_lower:
        return "Node服务"
    if "python" in cmd_lower:
        return "Python脚本"
    # 兜底：用进程名
    if cmd:
        return cmd[:40]
    return "未知进程"


# ============================================================
# 来源标题：把调用来源溯源体现到 TeleAgent 会话标题
# ============================================================
_SOURCE_PREFIXES = ("企微", "QQ", "密信", "机器人预热", "机器人视觉", "机器人", "星小辰机器人", "AI工厂", "子智能体", "脚本", "curl", "Node", "面板测试")

# 调用进程名 → 来源标签映射（当 session_title/UA 无法精确判定来源时，用 caller 反哺）
# 仅当 source_tag 仍为泛化标签（外部/子智能体/脚本/curl/Node）时才覆盖，避免误覆盖企微/QQ/密信
_CALLER_SOURCE_MAP = {
    "Reachy Mini": "机器人",
    "机器人视觉": "机器人视觉",
    "AI工厂": "AI工厂",
    "wecom-bot": "企微",
    "QQ适配器": "QQ",
    "量子密信适配器": "密信",
    "TeleAgent主程序": "子智能体",
    "面板测试": "面板测试",
}

# 会被 caller 反哺覆盖的泛化标签（精确渠道标签和 UA 识别标签不会被覆盖）
# 注意：curl/脚本/Node 已由 UA 精确识别，不应被 lsof caller 覆盖
# （万达云代理拦截所有流量，lsof 只能看到万达云/TeleAgent主程序，不是真正调用者）
_GENERIC_SOURCE_TAGS = {"外部", "子智能体"}


from datetime import datetime as _dt


def _now_date_time():
    """返回当前 (日期, 时间) 字符串，格式 YYYY-MM-DD / HH:MM"""
    now = _dt.now()
    return now.strftime("%Y-%m-%d"), now.strftime("%H:%M")


def build_source_session_title(source_tag, caller_name, raw_title):
    """构造带来源标识的会话标题，使 TeleAgent 会话名体现调用来源。

    标题格式（均按「来源|日期|时间」三段或已有业务标题保留）：
      - 企微/QQ/密信：原始标题已含渠道前缀 → 原样保留
      - 机器人(对话)：Reachy Mini对话|日期|时间
      - 机器人预热：  Reachy Mini预热|日期|时间
      - 机器人视觉：  Reachy Mini视觉|日期|时间
      - AI工厂：      AI工厂|日期|时间
      - 脚本：        脚本|调用进程|日期|时间
      - curl：        curl|日期|时间
      - 子智能体/Node/面板测试：原样保留
    """
    base = raw_title or "星小辰-子智能体"

    # ---- 企微/QQ/密信：原始标题已含渠道前缀，原样保留 ----
    for p in ("企微", "QQ", "密信"):
        if base == p or base.startswith(p + "|") or base.startswith(p + ":") or base.startswith(p + "："):
            return base

    # ---- Reachy Mini 对话：s2s 传入「星小辰机器人|时间」，改写为三段 ----
    if "星小辰机器人" in base or source_tag == "机器人":
        date_str, time_str = _now_date_time()
        return f"Reachy Mini对话|{date_str}|{time_str}"

    # ---- 其余自动拼接来源 ----
    if not source_tag:
        return base

    # 来源标签 → 第一段显示名
    DISPLAY = {
        "机器人预热": "Reachy Mini预热",
        "机器人视觉": "Reachy Mini视觉",
        "AI工厂":    "AI工厂",
    }
    first = DISPLAY.get(source_tag, source_tag)

    # 需要用日期+时间格式的来源标签
    _DT_SOURCES = {"机器人预热", "机器人视觉", "AI工厂", "脚本", "curl", "Node"}

    date_str, time_str = _now_date_time()
    dt_suffix = f"{date_str}|{time_str}"

    # 预热/视觉/AI工厂/脚本/curl/Node：统一用「显示名[|调用方]|日期|时间」
    if source_tag in _DT_SOURCES:
        if source_tag in ("脚本", "curl", "Node") and caller_name and caller_name not in ("未知", "未知进程", "8088代理自身") and caller_name != source_tag:
            return f"{first}|{caller_name}|{dt_suffix}"
        return f"{first}|{dt_suffix}"

    # 子智能体/面板测试/外部等：保留原基底拼接方式
    caller = ""
    if caller_name and caller_name not in ("未知", "未知进程", "8088代理自身") and caller_name != source_tag:
        caller = "|" + caller_name
    return f"{source_tag}{caller}|{base}"


def identify_caller_process(client_ip, client_port):
    """通过 lsof 反查连接 8088 的客户端进程，返回友好名称和 PID"""
    cache_key = (client_ip, client_port)
    now = time.time()

    with _caller_name_lock:
        cached = _caller_name_cache.get(cache_key)
        if cached and now - cached["time"] < _CALLER_CACHE_TTL:
            return cached["name"], cached["pid"], cached["cmd"]

    # 默认值
    name, pid, cmd = "未知", 0, ""

    try:
        # lsof -i :8088 查出所有连接到 8088 的进程（含客户端和服务端行）
        result = subprocess.run(
            ["lsof", "-i", f":{8088}", "-n", "-P"],
            capture_output=True, text=True, timeout=3, errors="replace"
        )
        if result.returncode == 0 and result.stdout:
            # 解析标准 lsof 输出格式：
            # COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
            # wandaclou 1748 lizhun 167u IPv4 ... TCP 127.0.0.1:58047->127.0.0.1:8088 (ESTABLISHED)
            # Python 73576 lizhun 8u IPv4 ... TCP 127.0.0.1:8088->127.0.0.1:58047 (ESTABLISHED)
            lines = result.stdout.strip().split("\n")
            target_port_str = str(client_port)
            for line in lines[1:]:  # 跳过表头
                parts = line.split()
                if len(parts) < 9:
                    continue
                proc_cmd = parts[0]
                proc_pid = parts[1]
                name_field = parts[8]  # NAME 列
                # 服务端行：127.0.0.1:8088->127.0.0.1:CLIENT_PORT
                # 客户端行：127.0.0.1:CLIENT_PORT->127.0.0.1:8088
                # 两种格式都匹配 client_port
                if target_port_str in name_field:
                    # 跳过监听行（LISTEN）
                    if "LISTEN" in name_field:
                        continue
                    # 找到匹配行，但需要区分：我们要的是客户端进程
                    # 客户端行的 NAME 格式是 ip:port->127.0.0.1:8088
                    # 服务端行的 NAME 格式是 127.0.0.1:8088->ip:port
                    # 取 -> 后面的端口，如果不是 8088，说明这是客户端
                    if "->" in name_field:
                        left, right = name_field.split("->", 1)
                        # right 格式: 127.0.0.1:8088 (ESTABLISHED)
                        right_port = right.split(":")[-1].split()[0] if ":" in right else ""
                        if right_port == "8088":
                            # 这是客户端进程
                            pid = proc_pid
                            cmd = proc_cmd
                            # 获取完整命令行
                            try:
                                ps_result = subprocess.run(
                                    ["ps", "-p", proc_pid, "-o", "command="],
                                    capture_output=True, text=True, timeout=2, errors="replace"
                                )
                                if ps_result.returncode == 0:
                                    cmd = ps_result.stdout.strip()
                            except:
                                pass
                            # 获取 cwd
                            cwd = ""
                            try:
                                lsof_cwd = subprocess.run(
                                    ["lsof", "-p", proc_pid, "-a", "-d", "cwd", "-Fn"],
                                    capture_output=True, text=True, timeout=2, errors="replace"
                                )
                                if lsof_cwd.returncode == 0:
                                    for cl in lsof_cwd.stdout.strip().split("\n"):
                                        if cl.startswith("n"):
                                            cwd = cl[1:]
                                            break
                            except:
                                pass
                            name = _friendly_caller_name(int(pid) if pid.isdigit() else 0, cmd, cwd)
                            break

        # ---- 万达云代理穿透 ----
        # 当 lsof 抓到的 caller 是万达云（wandacloud / 万达云），说明请求经系统代理转发，
        # 真正的调用方在 7892 端口侧。反查 7892 上的 TeleAgent / super-agent 进程。
        _is_wanda = "wanda" in (cmd or "").lower() or "万达" in (cmd or "") or "万达" in name or "wanda" in name.lower()
        if name in ("未知", "") or _is_wanda:
            penetrated = _penetrate_wandacloud_proxy()
            if penetrated:
                name, pid, cmd = penetrated
    except Exception:
        pass

    with _caller_name_lock:
        _caller_name_cache[cache_key] = {"name": name, "pid": pid, "cmd": cmd, "time": now}

    return name, pid, cmd


def _penetrate_wandacloud_proxy():
    """当 caller 是万达云代理时，反查 7892 端口上的真实调用进程。

    万达云（wandacloud）监听 7892，TeleAgent 的 Electron/Chromium 和 super-agent
    通过 7892 发出请求，万达云再转发到 127.0.0.1:8088。lsof -i :8088 只能看到万达云，
    所以需要查 lsof -i :7892 找到 TeleAgent 侧的 ESTABLISHED 连接。

    返回 (friendly_name, pid, cmd) 或 None。
    """
    try:
        result = subprocess.run(
            ["lsof", "-i", ":7892", "-n", "-P"],
            capture_output=True, text=True, timeout=3, errors="replace"
        )
        if result.returncode != 0 or not result.stdout:
            return None

        lines = result.stdout.strip().split("\n")
        candidates = []  # [(pid, cmd, cwd, score)]，score 越高优先级越高

        for line in lines[1:]:  # 跳过表头
            parts = line.split()
            if len(parts) < 9:
                continue
            proc_cmd = parts[0]
            proc_pid = parts[1]
            name_field = parts[8]

            # 只要 ESTABLISHED 连接，跳过 LISTEN/CLOSE_WAIT/FIN_WAIT 等
            # 注意：(ESTABLISHED) 在 parts[9]，不在 parts[8]
            if "LISTEN" in line or "ESTABLISHED" not in line:
                continue
            # 跳过万达云自身（支持中英文进程名）
            if "wanda" in proc_cmd.lower() or "万达" in proc_cmd:
                continue

            # 获取完整命令行
            full_cmd = proc_cmd
            try:
                ps_result = subprocess.run(
                    ["ps", "-p", proc_pid, "-o", "command="],
                    capture_output=True, text=True, timeout=2, errors="replace"
                )
                if ps_result.returncode == 0:
                    full_cmd = ps_result.stdout.strip()
            except Exception:
                pass

            # 获取 cwd
            cwd = ""
            try:
                lsof_cwd = subprocess.run(
                    ["lsof", "-p", proc_pid, "-a", "-d", "cwd", "-Fn"],
                    capture_output=True, text=True, timeout=2, errors="replace"
                )
                if lsof_cwd.returncode == 0:
                    for cl in lsof_cwd.stdout.strip().split("\n"):
                        if cl.startswith("n"):
                            cwd = cl[1:]
                            break
            except Exception:
                pass

            friendly = _friendly_caller_name(int(proc_pid) if proc_pid.isdigit() else 0, full_cmd, cwd)
            # 排除未知 / 万达云自身 / 8088代理自身
            if friendly in ("未知", "8088代理自身") or "wanda" in full_cmd.lower() or "万达" in full_cmd:
                continue

            # 优先级评分：
            #   第一梯队（直接调 8088 的应用）: s2s/AI工厂/wecom-bot/QQ/密信 → 90+
            #   第二梯队（TeleAgent 自身体系）: super-agent → 80, TeleAgent主程序 → 70
            #   第三梯队（Electron Helper 等框架进程）: → 60
            #   其他 → 30
            # 原则：具体应用进程优先于 TeleAgent 框架进程，
            #       因为 TeleAgent 的长连接只是保活，不是真正发 LLM 请求的
            score = 0
            cmd_lower = full_cmd.lower()
            if "speech-to-speech" in cmd_lower or "s2s" in cmd_lower or "reachy" in cmd_lower:
                score = 110  # Reachy Mini 语音对话，最可能是直接调 8088 的
            elif "ai_factory" in cmd_lower or "ai-factory" in cmd_lower or "streamlit" in cmd_lower:
                score = 105  # 本地 AI 工厂
            elif "super-agent" in cmd_lower:
                score = 100  # super-agent 是 AI 推理调度
            elif "teleagent" in cmd_lower or "opencowork" in cmd_lower:
                score = 70   # TeleAgent 主程序（长连接保活，优先级降低）
            elif "electron" in cmd_lower or "helper" in cmd_lower:
                score = 60  # Electron Helper / Network Service
            elif "wecom" in cmd_lower:
                score = 95  # 企微机器人
            elif "qq" in cmd_lower or "zmx" in cmd_lower:
                score = 95  # QQ/密信适配器
            else:
                score = 30

            candidates.append((proc_pid, full_cmd, friendly, score))

        if not candidates:
            return None

        # 取得分最高的候选
        candidates.sort(key=lambda x: x[3], reverse=True)
        best_pid, best_cmd, best_name, _ = candidates[0]
        return best_name, best_pid, best_cmd
    except Exception:
        return None


def get_recent_logs(limit=50):
    """获取最近的请求日志"""
    with _request_log_lock:
        return list(reversed(_request_log[-limit:]))


def read_disk_logs(date=None, limit=5000):
    """从磁盘 JSONL 读取请求日志（重启不丢）。date 为 YYYY-MM-DD，默认当天。"""
    try:
        path = _log_file_path()
        if date:
            path = os.path.join(LOG_DIR, f"requests-{date}.jsonl")
        if not os.path.exists(path):
            return []
        entries = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
        # 按时间倒序，取最新 limit 条
        entries.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
        return entries[:limit]
    except Exception:
        return []


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
# Tool Call 支持（让 s2s/机器人能通过 function calling 调用工具）
# ============================================================
# 8088 代理收到 OpenAI 格式的 tools 参数后，将工具描述注入到发给
# TeleAgent 的 prompt 中，LLM 用特殊标记输出工具调用，代理解析后
# 转成 OpenAI tool_calls 格式返回给调用方。

_TOOL_CALL_PATTERN = re.compile(r'\[TOOL_CALL\]\s*(\{.*?\})\s*\[/TOOL_CALL\]', re.DOTALL)


def _build_tool_prompt(tools):
    """将 OpenAI tools 列表转成注入给 LLM 的工具描述文本。"""
    if not tools:
        return None
    tool_descs = []
    for t in tools:
        if isinstance(t, dict):
            fn = t.get("function", t)
            name = fn.get("name", "")
            desc = fn.get("description", "")
            params = fn.get("parameters", {})
            params_str = json.dumps(params, ensure_ascii=False, indent=2)
            tool_descs.append(f"  - {name}: {desc}\n    参数: {params_str}")
    if not tool_descs:
        return None
    tool_text = "\n".join(tool_descs)
    return (
        f"你可以使用以下工具：\n\n{tool_text}\n\n"
        f"当你需要调用工具时，你的回复应该只包含以下格式，不要输出任何其他文字：\n"
        f"[TOOL_CALL]{{\"name\": \"工具名称\", \"arguments\": {{...}}}}[/TOOL_CALL]\n"
        f"当你不需要调用工具时，正常回复即可。"
    )


def _parse_tool_calls_from_text(text):
    """从 LLM 回复文本中解析 [TOOL_CALL]...[/TOOL_CALL] 标记。
    返回 (clean_text, tool_calls) 其中 tool_calls 为 OpenAI Chat Completions 格式。
    """
    matches = list(_TOOL_CALL_PATTERN.finditer(text))
    if not matches:
        return text, []
    tool_calls = []
    clean_parts = []
    last_end = 0
    for i, m in enumerate(matches):
        clean_parts.append(text[last_end:m.start()])
        last_end = m.end()
        try:
            tc = json.loads(m.group(1))
            tool_calls.append({
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False)
                }
            })
        except (json.JSONDecodeError, KeyError):
            pass
    clean_parts.append(text[last_end:])
    clean_text = "".join(clean_parts).strip()
    return clean_text, tool_calls


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
    """在 super-agent 会话列表中匹配会话，返回最新更新的 session_id（无则 None）
    只匹配同目录，避免误复用其他目录的同名会话。
    
    匹配规则：
    - 标题含"|"时按前缀匹配：查找已有会话中标题以传入 title 的"|"前部分开头的会话
      （如传入"星小辰机器人|08:35"可匹配"星小辰机器人|08:30"），实现时间变化仍复用
    - 标题不含"|"时按精确匹配
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
    # 提取前缀（用于含"|"的标题前缀匹配）
    prefix = title.split("|", 1)[0] if "|" in title else None
    matched = []
    for s in sessions:
        s_title = s.get("title", "") or ""
        if prefix:
            # 前缀匹配：已有会话标题也含"|"且前缀一致
            s_prefix = s_title.split("|", 1)[0] if "|" in s_title else None
            if s_prefix != prefix:
                continue
        else:
            if s_title != title:
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
    
    对于含"|"的标题，缓存 key 统一用前缀（"|"前的部分），使时间变化仍命中缓存。
    """
    if not title:
        return create_session(directory=directory, title=None), False

    # 缓存 key：含"|"时用前缀，否则用完整标题
    cache_key = title.split("|", 1)[0] if "|" in title else title

    now = time.time()
    # 1. 本地缓存命中
    with _session_cache_lock:
        cached = _session_cache.get(cache_key)
        if cached and now - cached[1] < SESSION_CACHE_TTL:
            return cached[0], True

    # 2. 按标题查已有会话
    sid = get_session_by_title(title, directory=directory)
    if sid:
        with _session_cache_lock:
            _session_cache[cache_key] = (sid, now)
        return sid, True

    # 3. 新建会话（进程内同名互斥，避免并发重复创建）
    with _session_cache_lock:
        _title_create_locks.setdefault(cache_key, threading.Lock())
        create_lock = _title_create_locks[cache_key]
    with create_lock:
        # 双检：等待锁期间可能已被其他线程创建
        _sid = get_session_by_title(title, directory=directory)
        if _sid:
            with _session_cache_lock:
                _session_cache[cache_key] = (_sid, time.time())
            return _sid, True
        sid = create_session(directory=directory, title=title)
        if sid:
            with _session_cache_lock:
                _session_cache[cache_key] = (sid, time.time())
        return sid, False


def send_prompt_async(session_id, messages, directory=DEFAULT_DIRECTORY,
                      provider_id=DEFAULT_PROVIDER, model_id=DEFAULT_MODEL,
                      tools=None, session_reused=False):
    """异步发送消息到 super-agent 会话

    tools: OpenAI 格式的工具列表，会注入到 prompt 中让 LLM 知道可用工具。
    session_reused: 会话是否复用。复用时只发 system+tools 和最新一条消息，
                   避免与 TeleAgent 会话自身的历史双重累积导致上下文爆炸。
    """
    # 将 OpenAI messages 格式转换为 super-agent 的 prompt
    # TeleAgent 会话本身维护上下文历史，所以：
    #   - 新会话：发送完整历史（建立上下文）
    #   - 复用会话：只发 system+tools + 最新一条消息（避免双重累积）

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
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    # 多模态图片：提取 base64 → 存临时文件 → 在 prompt 中插入文件路径
                    img_url = part.get("image_url", {}).get("url", "") if isinstance(part.get("image_url"), dict) else part.get("image_url", "")
                    if img_url.startswith("data:image"):
                        # 解析 data URI: data:image/jpeg;base64,xxxx
                        try:
                            header, b64data = img_url.split(",", 1)
                            img_bytes = base64.b64decode(b64data)
                            img_path = os.path.join("/tmp/reachy_vision", f"proxy_{int(time.time()*1000)}.jpg")
                            os.makedirs(os.path.dirname(img_path), exist_ok=True)
                            with open(img_path, "wb") as f:
                                f.write(img_bytes)
                            text_parts.append(f"请用 image_understanding 工具查看图片 {img_path} 并回答问题。")
                            print(f"[proxy] Multimodal image saved to {img_path} ({len(img_bytes)} bytes)", flush=True)
                        except Exception as e:
                            print(f"[proxy] Failed to save multimodal image: {e}", flush=True)
                            text_parts.append("[图片解析失败]")
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
        elif role == "tool":
            # 工具返回结果，作为对话上下文注入
            user_messages.append(f"[tool_result] {content}")

    # 如果有工具定义，注入到 system 消息
    if tools:
        tool_prompt = _build_tool_prompt(tools)
        if tool_prompt:
            if system_content:
                system_content = system_content + "\n\n" + tool_prompt
            else:
                system_content = tool_prompt

    # 会话复用时，TeleAgent 已有完整对话历史。
    # 只发 system+tools 和最新一条消息，避免上下文双重累积。
    if session_reused and len(user_messages) > 1:
        user_messages = [user_messages[-1]]

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


# ============================================================
# 权限/问题确认回复（机器人侧"群里确认/拒绝"）
# ============================================================
def register_pending_confirmation(conf_id, conf_type, session_id, description="", tool=""):
    """登记一个待确认请求（permission.asked / question.asked 事件触发）

    幂等设计：同一 conf_id 可能被全局监听器与请求链路 SSE 双通道捕获，
    若已登记则只补充缺失字段（title 等），不刷新 time、不触发重复消费，
    避免机器人侧重复推送通知、重复 reply 导致 404。
    """
    # 自动补充 session_title 关联（若有映射）
    with _session_title_map_lock:
        title = _session_title_map.get(session_id, "")
    with _pending_confirmations_lock:
        existing = _pending_confirmations.get(conf_id)
        if existing is not None:
            # 已存在：仅补全缺失信息（双通道重复捕获时保持首次登记为准）
            if title and not existing.get("title"):
                existing["title"] = title
            if not existing.get("session_id"):
                existing["session_id"] = session_id
            if not existing.get("description") and description:
                existing["description"] = description
            if not existing.get("tool") and tool:
                existing["tool"] = tool
            if not existing.get("type") and conf_type:
                existing["type"] = conf_type
            return False  # 表示已存在（重复捕获）
        _pending_confirmations[conf_id] = {
            "type": conf_type,          # "permission" | "question"
            "session_id": session_id,
            "title": title,             # 关联的会话标题（机器人侧可据此匹配推送）
            "description": description,
            "tool": tool,
            "time": time.time(),
        }
        return True  # 首次登记


def get_pending_confirmation(conf_id):
    """查询待确认请求信息"""
    with _pending_confirmations_lock:
        return _pending_confirmations.get(conf_id)


def list_pending_confirmations():
    """列出所有待确认请求（供控制台/调试）"""
    with _pending_confirmations_lock:
        return dict(_pending_confirmations)


def _prune_pending_confirmations():
    """清理超时未处理的待确认请求"""
    now = time.time()
    with _pending_confirmations_lock:
        expired = [k for k, v in _pending_confirmations.items()
                   if now - v.get("time", 0) > _PENDING_CONFIRM_TTL]
        for k in expired:
            _pending_confirmations.pop(k, None)


def reply_permission(permission_id, reply):
    """回复权限确认：reply 取值 once / always / reject"""
    if reply not in ("once", "always", "reject"):
        return False, "reply must be 'once', 'always' or 'reject'"
    status, resp, _ = sa_request(
        "POST", f"/permission/{permission_id}/reply",
        body={"reply": reply}, timeout=10
    )
    if status in (200, 204):
        with _pending_confirmations_lock:
            _pending_confirmations.pop(permission_id, None)
        return True, resp
    return False, f"HTTP {status}: {resp[:200]}"


def reply_question(question_id, answers):
    """回复问题：answers 为答案字符串列表（单选/多选）"""
    if not isinstance(answers, list) or not answers:
        return False, "answers must be a non-empty list"
    status, resp, _ = sa_request(
        "POST", f"/question/{question_id}/reply",
        body={"answers": answers}, timeout=10
    )
    if status in (200, 204):
        with _pending_confirmations_lock:
            _pending_confirmations.pop(question_id, None)
        return True, resp
    return False, f"HTTP {status}: {resp[:200]}"


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
        # 待确认请求（permission.asked / question.asked 事件）
        self.on_confirmation = None   # 回调：conf_id, conf_type, description, tool
        self.pending_confirmations = []  # 本监听器收集到的待确认请求

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

        elif etype == "permission.asked":
            # AI 需要用户授权（访问外部目录、执行命令等）
            # super-agent 的 permission.asked 事件结构：
            # {id, sessionID, permission, patterns, always, tool}
            permission_id = props.get("id") or props.get("permissionID") or props.get("permissionId") or ""
            perm_type = props.get("permission") or ""
            patterns = props.get("patterns") or []
            always = props.get("always") or []
            tool_raw = props.get("tool") or props.get("toolID") or ""
            # 构造人类可读描述
            desc = props.get("description") or props.get("prompt") or ""
            if not desc:
                parts = []
                if perm_type:
                    parts.append(f"权限类型: {perm_type}")
                if patterns:
                    parts.append(f"路径: {', '.join(patterns)}")
                elif always:
                    parts.append(f"路径: {', '.join(always)}")
                desc = " | ".join(parts) if parts else ""
            # tool 可能是 dict（含 messageID/callID）也可能是字符串
            if isinstance(tool_raw, dict):
                tool = tool_raw.get("callID") or tool_raw.get("name") or ""
            else:
                tool = str(tool_raw) if tool_raw else ""
            if permission_id:
                self.pending_confirmations.append({
                    "id": permission_id, "type": "permission",
                    "description": desc, "tool": tool,
                })
                is_new = register_pending_confirmation(permission_id, "permission",
                                                       self.session_id, desc, tool)
                # 仅首次登记时触发回调（避免双通道重复注入 SSE 确认）
                if is_new and self.on_confirmation:
                    self.on_confirmation(permission_id, "permission", desc, tool)

        elif etype == "question.asked":
            # AI 向用户提问（需要选择答案）
            question_id = props.get("id") or props.get("questionID") or props.get("questionId") or ""
            desc = props.get("description") or props.get("prompt") or ""
            tool_raw = props.get("tool") or props.get("toolID") or ""
            if isinstance(tool_raw, dict):
                tool = tool_raw.get("callID") or tool_raw.get("name") or ""
            else:
                tool = str(tool_raw) if tool_raw else ""
            if question_id:
                self.pending_confirmations.append({
                    "id": question_id, "type": "question",
                    "description": desc, "tool": tool,
                })
                is_new = register_pending_confirmation(question_id, "question",
                                                       self.session_id, desc, tool)
                # 仅首次登记时触发回调（避免双通道重复注入 SSE 确认）
                if is_new and self.on_confirmation:
                    self.on_confirmation(question_id, "question", desc, tool)

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
# 常驻全局确认监听器（多轮确认支持）
# ============================================================
class GlobalConfirmationListener:
    """后台线程：常驻连接 super-agent 的 /global/event 事件流，
    捕获所有会话的 permission.asked / question.asked 事件并登记到
    _pending_confirmations，不依赖任何 chat/completions 请求的 SSE 连接。

    这样即使一次请求的 SSE 连接在第一次确认后关闭，后续轮次的确认
    （AI 确认后继续执行又触发新敏感操作）也能被捕获，机器人轮询
    /api/permission/pending 即可发现并通知用户。断线自动重连。
    """

    RECONNECT_DELAY = 5  # 断线重连间隔（秒）

    def __init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._seen = set()  # 已登记过的 conf_id（幂等去重）

    def stop(self):
        self._stop.set()

    def _connect_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((SUPER_AGENT_HOST, SUPER_AGENT_PORT))
        sock.sendall(self._make_request())
        return sock

    def _make_request(self):
        headers = sign_request("GET", "/global/event")
        headers["Accept"] = "text/event-stream"
        headers["Host"] = f"{SUPER_AGENT_HOST}:{SUPER_AGENT_PORT}"
        req = f"GET /global/event HTTP/1.1\r\n"
        for k, v in headers.items():
            req += f"{k}: {v}\r\n"
        req += "Connection: keep-alive\r\n\r\n"
        return req.encode()

    def _handle_event(self, event):
        payload = event.get("payload", {})
        etype = payload.get("type", "")
        props = payload.get("properties", {})
        session_id = props.get("sessionID", "")

        conf_id = None
        conf_type = None
        desc = ""

        if etype == "permission.asked":
            conf_type = "permission"
            conf_id = props.get("id") or props.get("permissionID") or props.get("permissionId") or ""
            perm_type = props.get("permission") or ""
            patterns = props.get("patterns") or []
            always = props.get("always") or []
            desc = props.get("description") or props.get("prompt") or ""
            if not desc:
                parts = []
                if perm_type:
                    parts.append(f"权限类型: {perm_type}")
                if patterns:
                    parts.append(f"路径: {', '.join(patterns)}")
                elif always:
                    parts.append(f"路径: {', '.join(always)}")
                desc = " | ".join(parts) if parts else ""
        elif etype == "question.asked":
            conf_type = "question"
            conf_id = props.get("id") or props.get("questionID") or props.get("questionId") or ""
            desc = props.get("description") or props.get("prompt") or ""

        if not conf_id or not conf_type:
            return

        with self._lock:
            if conf_id in self._seen:
                return
            self._seen.add(conf_id)

        register_pending_confirmation(conf_id, conf_type, session_id, desc, "")
        # 补充 session_title 关联（若已知）
        with _session_title_map_lock:
            title = _session_title_map.get(session_id, "")
        if title:
            with _pending_confirmations_lock:
                if conf_id in _pending_confirmations:
                    _pending_confirmations[conf_id]["title"] = title
        print(f"[GLOBAL-CONF] 常驻监听器捕获确认: id={conf_id} type={conf_type} "
              f"session={session_id[:20]} title={title[:30]} desc={desc[:80]}", file=sys.stderr)

    def run(self):
        """主循环：连接 → 监听 → 断线重连"""
        while not self._stop.is_set():
            sock = None
            try:
                sock = self._connect_socket()
                buffer = ""
                header_parsed = False
                print("[GLOBAL-CONF] 常驻确认监听器已连接 /global/event", file=sys.stderr)
                while not self._stop.is_set():
                    ready = select.select([sock], [], [], 1)
                    if not ready[0]:
                        continue
                    try:
                        chunk = sock.recv(8192)
                    except socket.timeout:
                        continue
                    except Exception:
                        break
                    if not chunk:
                        break
                    buffer += chunk.decode("utf-8", errors="replace")
                    if not header_parsed and "\r\n\r\n" in buffer:
                        _, buffer = buffer.split("\r\n\r\n", 1)
                        header_parsed = True
                    if not header_parsed:
                        continue
                    lines = buffer.split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        event_data = line[5:].strip()
                        if not event_data:
                            continue
                        try:
                            int(event_data, 16)
                            continue
                        except ValueError:
                            pass
                        try:
                            event = json.loads(event_data)
                        except json.JSONDecodeError:
                            continue
                        try:
                            self._handle_event(event)
                        except Exception as e:
                            print(f"[GLOBAL-CONF] 处理事件异常: {e}", file=sys.stderr)
            except Exception as e:
                print(f"[GLOBAL-CONF] 监听异常: {e}", file=sys.stderr)
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            time.sleep(self.RECONNECT_DELAY)


_global_conf_listener = None


def start_global_confirmation_listener():
    """启动常驻确认监听器（幂等，只启动一次）"""
    global _global_conf_listener
    if _global_conf_listener is not None:
        return
    _global_conf_listener = GlobalConfirmationListener()
    t = threading.Thread(target=_global_conf_listener.run, daemon=True)
    t.start()
    print("[GLOBAL-CONF] 常驻确认监听线程已启动", file=sys.stderr)


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

        # session_id 过滤：只关注当前会话的事件
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

        elif etype == "permission.asked":
            # AI 需要用户授权（访问外部目录、执行命令等）
            # super-agent 事件结构: {id, sessionID, permission, patterns, always, tool}
            permission_id = props.get("id") or props.get("permissionID") or props.get("permissionId") or ""
            perm_type = props.get("permission") or ""
            patterns = props.get("patterns") or []
            always = props.get("always") or []
            tool_raw = props.get("tool") or props.get("toolID") or ""
            # 构造人类可读描述
            desc = props.get("description") or props.get("prompt") or ""
            if not desc:
                parts = []
                if perm_type:
                    parts.append(f"权限类型: {perm_type}")
                if patterns:
                    parts.append(f"路径: {', '.join(patterns)}")
                elif always:
                    parts.append(f"路径: {', '.join(always)}")
                desc = " | ".join(parts) if parts else ""
            # tool 可能是 dict（含 messageID/callID）也可能是字符串
            if isinstance(tool_raw, dict):
                tool = tool_raw.get("callID") or tool_raw.get("name") or ""
            else:
                tool = str(tool_raw) if tool_raw else ""
            if permission_id:
                self.pending_confirmations.append({
                    "id": permission_id, "type": "permission",
                    "description": desc, "tool": tool,
                })
                is_new = register_pending_confirmation(permission_id, "permission",
                                                       self.session_id, desc, tool)
                # 仅首次登记时触发回调（避免双通道重复注入 SSE 确认）
                if is_new and self.on_confirmation:
                    self.on_confirmation(permission_id, "permission", desc, tool)

        elif etype == "question.asked":
            # AI 向用户提问（需要选择答案）
            question_id = props.get("id") or props.get("questionID") or props.get("questionId") or ""
            desc = props.get("description") or props.get("prompt") or ""
            tool_raw = props.get("tool") or props.get("toolID") or ""
            if isinstance(tool_raw, dict):
                tool = tool_raw.get("callID") or tool_raw.get("name") or ""
            else:
                tool = str(tool_raw) if tool_raw else ""
            if question_id:
                self.pending_confirmations.append({
                    "id": question_id, "type": "question",
                    "description": desc, "tool": tool,
                })
                is_new = register_pending_confirmation(question_id, "question",
                                                       self.session_id, desc, tool)
                # 仅首次登记时触发回调（避免双通道重复注入 SSE 确认）
                if is_new and self.on_confirmation:
                    self.on_confirmation(question_id, "question", desc, tool)


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
        elif path == "/api/permission/reply":
            self._handle_permission_reply()
        elif path == "/api/permission/pending":
            self._handle_permission_pending()
        else:
            self._send_json(404, {"error": {"message": "Not found", "type": "invalid_request"}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/v1/chat/completions":
            self._handle_chat_completions()
        elif path == "/v1/embeddings":
            self._handle_embeddings()
        elif path == "/api/test":
            self._handle_api_test()
        elif path == "/api/default-model":
            self._handle_api_default_model()
        elif path == "/api/permission/reply":
            self._handle_permission_reply()
        elif path == "/api/permission/inject":
            self._handle_permission_inject()
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
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        limit = int(qs.get("limit", ["100"])[0])
        # 参数 date=YYYY-MM-DD 指定查某天磁盘日志；默认查当天
        date_arg = qs.get("date", [None])[0]
        logs = get_recent_logs(limit)
        disk_logs = read_disk_logs(date=date_arg)
        # 磁盘日志已含当天全部请求（含内存中的），若指定了 date 则直接返回磁盘日志
        if date_arg:
            self._send_json(200, {"logs": disk_logs[:limit], "total": len(disk_logs)})
            return
        # 未指定 date：合并内存日志与当天磁盘日志（磁盘为准，避免重复）
        self._send_json(200, {"logs": disk_logs[:limit] if disk_logs else logs, "total": len(disk_logs or logs)})

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

    # ======== 权限/问题确认 API（机器人侧"群里确认/拒绝"） ========

    def _handle_permission_pending(self):
        """GET /api/permission/pending — 列出当前所有待确认请求"""
        _prune_pending_confirmations()
        pending = list_pending_confirmations()
        result = []
        for conf_id, info in pending.items():
            result.append({
                "id": conf_id,
                "type": info.get("type", ""),
                "session_id": info.get("session_id", ""),
                "title": info.get("title", ""),
                "description": info.get("description", ""),
                "tool": info.get("tool", ""),
                "time": info.get("time", 0),
            })
        self._send_json(200, {"pending": result, "count": len(result)})

    def _handle_permission_reply(self):
        """POST /api/permission/reply — 机器人转发用户的确认/拒绝

        Body:
          {
            "id": "per_xxxx",           # 权限ID 或 问题ID
            "reply": "once",             # 权限: once|always|reject
            "answers": ["选项1"],         # 问题: 答案列表（可选）
            "text": "用户附加反馈"         # 可选
          }
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            req_data = json.loads(body)
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        conf_id = req_data.get("id", "").strip()
        if not conf_id:
            self._send_json(400, {"error": "id is required (permissionID or questionID)"})
            return

        # 查询待确认类型
        conf_info = get_pending_confirmation(conf_id)
        conf_type = conf_info.get("type") if conf_info else None

        # 若注册表中没有，根据 ID 前缀推断
        if not conf_type:
            if conf_id.startswith("per_"):
                conf_type = "permission"
            elif conf_id.startswith("que_"):
                conf_type = "question"
            else:
                self._send_json(404, {"error": f"No pending confirmation for id: {conf_id}"})
                return

        if conf_type == "permission":
            reply = req_data.get("reply", "reject")
            ok, detail = reply_permission(conf_id, reply)
            if ok:
                self._send_json(200, {"success": True, "id": conf_id, "reply": reply})
            else:
                self._send_json(500, {"error": detail})
        elif conf_type == "question":
            answers = req_data.get("answers") or [req_data.get("reply", "")]
            ok, detail = reply_question(conf_id, answers)
            if ok:
                self._send_json(200, {"success": True, "id": conf_id, "answers": answers})
            else:
                self._send_json(500, {"error": detail})
        else:
            self._send_json(400, {"error": f"Unknown confirmation type: {conf_type}"})

    def _handle_permission_inject(self):
        """POST /api/permission/inject — 本地测试专用：注入一条模拟确认，
        用于验证 8088 → QQ适配器轮询 → QQ推送 的完整链路（不依赖 AI 是否弹确认）。

        Body:
          {
            "type": "permission",           # permission | question
            "title": "QQ|私聊|李四|2026-08-20 07:46",   # 会话标题（带 QQ| 前缀才会被轮询推送）
            "description": "权限类型: external_directory | 路径: /test-inject/*"
          }

        安全限制：仅允许本机（127.0.0.1 / ::1）调用，禁止局域网/外网访问，
        防止任意设备向 QQ 会话注入虚假确认通知。
        """
        client_ip = self.client_address[0] if self.client_address else ""
        if client_ip not in ("127.0.0.1", "::1"):
            log_request(f"WARN /api/permission/inject 被拒绝访问: 来源 {client_ip}（仅允许本机）")
            self._send_json(403, {"error": "inject endpoint is localhost-only"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            req_data = json.loads(body)
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return
        conf_type = req_data.get("type", "permission")
        title = req_data.get("title", "")
        description = req_data.get("description", "测试注入确认")
        conf_id = f"per_test_inject_{int(time.time()*1000)}" if conf_type == "permission" else f"que_test_inject_{int(time.time()*1000)}"
        with _pending_confirmations_lock:
            _pending_confirmations[conf_id] = {
                "type": conf_type,
                "session_id": "ses_test_inject",
                "title": title,
                "description": description,
                "tool": "test-inject",
                "time": time.time(),
            }
        self._send_json(200, {"success": True, "id": conf_id, "title": title})

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

    def _handle_embeddings(self):
        """处理 embedding 请求 - 转发到 Ollama OpenAI 兼容 API"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            req_data = json.loads(body)
        except Exception as e:
            self._send_json(400, {"error": {"message": f"Invalid JSON: {e}", "type": "invalid_request"}})
            return

        model = req_data.get("model", "")
        input_text = req_data.get("input", "")

        if not model:
            self._send_json(400, {"error": {"message": "model is required", "type": "invalid_request"}})
            return
        if not input_text:
            self._send_json(400, {"error": {"message": "input is required", "type": "invalid_request"}})
            return

        # Parse provider/model
        if "/" in model:
            provider_id, model_id = model.split("/", 1)
        else:
            provider_id, model_id = "", model

        # Route to appropriate backend
        if provider_id == "ollama-local":
            # Forward to Ollama OpenAI-compatible endpoint
            ollama_url = "http://127.0.0.1:11434/v1/embeddings"
            # Ollama expects model name without provider prefix
            ollama_model = model_id
            # Normalize: remove :latest suffix if present for ollama API
            ollama_req = {"model": ollama_model, "input": input_text}
            try:
                req_body = json.dumps(ollama_req).encode("utf-8")
                req = urllib.request.Request(ollama_url, data=req_body, method="POST")
                req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_data = json.loads(resp.read())
                    self._send_json(200, resp_data)
                    return
            except urllib.error.HTTPError as e:
                error_body = e.read().decode("utf-8", errors="replace")
                self._send_json(e.code, {"error": {"message": f"Ollama error: {error_body}", "type": "upstream_error"}})
                return
            except Exception as e:
                self._send_json(500, {"error": {"message": f"Ollama request failed: {e}", "type": "server_error"}})
                return
        elif provider_id == "qwen36-local":
            # Forward to vLLM embedding service
            vllm_url = "http://106.0.4.142:51211/v1/embeddings"
            vllm_model = model_id
            vllm_req = {"model": vllm_model, "input": input_text}
            try:
                req_body = json.dumps(vllm_req).encode("utf-8")
                req = urllib.request.Request(vllm_url, data=req_body, method="POST")
                req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_data = json.loads(resp.read())
                    self._send_json(200, resp_data)
                    return
            except Exception as e:
                self._send_json(500, {"error": {"message": f"vLLM embedding failed: {e}", "type": "server_error"}})
                return
        else:
            self._send_json(404, {"error": {"message": f"Embedding not supported for provider: {provider_id}", "type": "invalid_request"}})

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
        tools = req_data.get("tools", None)  # 提取 tools 参数
        if tools:
            tool_names = [t.get("function", t).get("name", "?") for t in tools if isinstance(t, dict)]
            print(f"[TOOLS] 收到 {len(tools)} 个工具: {tool_names}", file=sys.stderr)

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

        # ===== 提取 prompt 摘要用于来源判定与日志 =====
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

        # ===== 提取请求来源信息 =====
        client_ip = self.client_address[0] if self.client_address else "unknown"
        client_port = self.client_address[1] if self.client_address else 0
        user_agent = self.headers.get("User-Agent", "")
        # 读取自定义会话标题（机器人可传"姓名 | 技能 | 时间"）
        raw_session_title = req_data.get("session_title") or "星小辰-子智能体"
        # 识别业务渠道来源：企微/QQ/量子密信/面板测试/TeleAgent主程序/外部脚本/机器人预热
        source_tag = "外部"
        source_detail = ""
        # Reachy Mini warmup 判定：OpenAI Python SDK + prompt 为 "Hello" + 无 session_title 或默认标题
        is_warmup = (
            prompt_preview.strip().lower() == "hello"
            and "openai" in user_agent.lower()
            and (not req_data.get("session_title") or req_data.get("session_title") == "星小辰-子智能体")
        )
        if is_warmup:
            source_tag = "机器人预热"
            source_detail = "Reachy Mini s2s warmup"
        elif raw_session_title:
            st_lower = raw_session_title.lower()
            if st_lower.startswith("企微") or "wecom" in st_lower:
                source_tag = "企微"
                source_detail = raw_session_title
            elif st_lower.startswith("qq") or st_lower.startswith("qq|"):
                source_tag = "QQ"
                source_detail = raw_session_title
            elif st_lower.startswith("密信") or "zmx" in st_lower:
                source_tag = "密信"
                source_detail = raw_session_title
            elif "星小辰机器人" in raw_session_title or "机器人" in raw_session_title:
                source_tag = "机器人"
                source_detail = raw_session_title
            elif st_lower.startswith("console-test"):
                source_tag = "面板测试"
                source_detail = raw_session_title
            # 注意：默认值"星小辰-子智能体"不再标为"子智能体"来源，
            # 让 UA / caller 识别来判定真实来源（curl/脚本/AI工厂等）
        if not source_detail:
            source_detail = raw_session_title or ""
        # User-Agent 辅助识别
        if not is_warmup:  # warmup 已标注，跳过 UA 辅助识别
            if "python-requests" in user_agent.lower() or "python-urllib" in user_agent.lower():
                if source_tag == "外部":
                    source_tag = "脚本"
            elif "curl" in user_agent.lower():
                if source_tag == "外部":
                    source_tag = "curl"
            elif "node" in user_agent.lower() or "axios" in user_agent.lower():
                if source_tag == "外部":
                    source_tag = "Node"

        # ===== lsof 反查调用进程 =====
        caller_name, caller_pid, caller_cmd = identify_caller_process(client_ip, client_port)

        # ===== caller 反哺来源标签 =====
        # 当 source_tag 是泛化标签时，用 lsof 识别到的进程名精确到具体来源
        # （Reachy Mini → 机器人、AI工厂 → AI工厂 等），同时补全 source_detail
        if source_tag in _GENERIC_SOURCE_TAGS and caller_name in _CALLER_SOURCE_MAP:
            refined = _CALLER_SOURCE_MAP[caller_name]
            # 机器人预热是更细粒度，保留不被覆盖
            if not (source_tag == "机器人预热" and refined == "机器人"):
                source_tag = refined
                if not source_detail or source_detail == raw_session_title:
                    source_detail = caller_name

        # ===== 构造带来源前缀的会话标题 =====
        # 未传标题时来源前缀（脚本/curl/预热/子智能体）直接体现到会话名；
        # 原始标题已含来源（企微/QQ/密信等）则原样保留。
        session_title = build_source_session_title(source_tag, caller_name, raw_session_title)

        # 创建会话（带标题时按标题复用，避免机器人一句话开一个会话）
        session_id, session_reused = get_or_create_session(
            directory=DEFAULT_DIRECTORY, title=session_title
        )
        if not session_id:
            self._send_json(500, {"error": {"message": "Failed to create session", "type": "server_error"}})
            return

        # 登记 session_id -> session_title 映射（供确认事件关联机器人侧会话）
        with _session_title_map_lock:
            _session_title_map[session_id] = session_title
            # 防止无限增长，只保留最近 200 条
            if len(_session_title_map) > 200:
                oldest_keys = list(_session_title_map.keys())[:len(_session_title_map) - 200]
                for k in oldest_keys:
                    _session_title_map.pop(k, None)

        # 记录请求开始
        log_entry = {
            "id": request_id,
            "timestamp": time.time(),
            "model": model,
            "stream": stream,
            "session_id": session_id,
            "session_title": session_title,
            "session_reused": session_reused,
            "source": source_tag,
            "source_detail": source_detail,
            "client_ip": client_ip,
            "user_agent": user_agent[:80],
            "caller": caller_name,
            "caller_pid": caller_pid,
            "caller_cmd": caller_cmd[:120],
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
                                          stream_options, tools, session_reused)
        else:
            self._handle_non_streaming_logged(session_id, messages, request_id, created,
                                               provider_id, model_id, log_entry, start_time,
                                               tools, session_reused)

    def _handle_streaming_logged(self, session_id, messages, request_id, created,
                                  provider_id, model_id, log_entry, start_time,
                                  stream_options=None, tools=None, session_reused=False):
        """流式响应（带日志），支持 tool call 检测"""
        # 发送 SSE 响应头
        self._send_sse_headers()
        model_name = f"{provider_id}/{model_id}"
        first_chunk_sent = False
        collected_text = []
        collected_tokens = None
        has_error = False
        stream_options = stream_options or {}

        # Tool call 检测状态
        # 当 tools 参数存在时，始终缓冲完整响应再解析（LLM 可能在 tool call 前加对话文字）
        _has_tools = bool(tools)
        _buffer = []                  # 缓冲的文本片段

        def _emit_content(text):
            """发送 content delta"""
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

        def send_delta(text):
            if not text:
                return

            if _has_tools:
                # 有 tools 时始终缓冲，等完整响应再解析
                _buffer.append(text)
                return

            # 无 tools 时正常流式
            _emit_content(text)

        def send_complete():
            nonlocal collected_tokens, first_chunk_sent

            # 有 tools 时：缓冲了完整响应，现在解析
            if _has_tools and _buffer:
                full_text = "".join(_buffer)
                clean_text, tool_calls = _parse_tool_calls_from_text(full_text)

                if tool_calls:
                    # 先发送 tool_call 前的对话文本（如"好，我看看哦~"）
                    # _parse_tool_calls_from_text 返回的 clean_text 是去掉 [TOOL_CALL] 标记后的剩余文本
                    if clean_text and clean_text.strip():
                        if not first_chunk_sent:
                            first_chunk = {
                                "id": request_id, "object": "chat.completion.chunk",
                                "created": created, "model": model_name,
                                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
                            }
                            self._send_sse_chunk(first_chunk)
                            first_chunk_sent = True
                        content_chunk = {
                            "id": request_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "choices": [{"index": 0, "delta": {"content": clean_text}, "finish_reason": None}]
                        }
                        self._send_sse_chunk(content_chunk)
                    # 然后发送 tool_calls delta
                    if not first_chunk_sent:
                        first_chunk = {
                            "id": request_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
                        }
                        self._send_sse_chunk(first_chunk)
                        first_chunk_sent = True
                    for i, tc in enumerate(tool_calls):
                        chunk = {
                            "id": request_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "choices": [{"index": 0, "delta": {
                                "tool_calls": [{
                                    "index": i,
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": tc["function"]
                                }]
                            }, "finish_reason": None}]
                        }
                        self._send_sse_chunk(chunk)
                    end_chunk = {
                        "id": request_id, "object": "chat.completion.chunk",
                        "created": created, "model": model_name,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
                    }
                    self._send_sse_chunk(end_chunk)
                    # usage chunk
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
                            "choices": [], "usage": usage,
                        }
                        self._send_sse_chunk(usage_chunk)
                    self._send_sse_end()
                    log_entry["status"] = "success"
                    log_entry["response_preview"] = f"[tool_calls: {len(tool_calls)}]"
                    log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
                    update_stats(model=model_name, streaming=True)
                    log_request_to_disk(log_entry)
                    return
                else:
                    # 没有检测到 tool calls，把缓冲文本作为正常 content 发送
                    _emit_content(clean_text or full_text)
                    _buffer.clear()

            # 正常完成
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
            log_request_to_disk(log_entry)

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
            log_request_to_disk(log_entry)

        # 权限/问题确认事件：以特殊 chunk 推给调用方（机器人据此提醒用户在群里确认）
        def send_confirmation(conf_id, conf_type, desc, tool):
            print(f"[CONF] 发送确认请求: id={conf_id} type={conf_type} desc={str(desc)[:80]}", file=sys.stderr)
            conf_chunk = {
                "id": request_id, "object": "chat.completion.chunk",
                "created": created, "model": model_name,
                "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                "confirmation": {
                    "id": conf_id,
                    "type": conf_type,   # "permission" | "question"
                    "description": desc,
                    "tool": tool,
                    "session_id": session_id,
                },
            }
            try:
                self._send_sse_chunk(conf_chunk)
            except Exception as e:
                print(f"[CONF] confirmation chunk 发送失败: {e}", file=sys.stderr)

        listener = StreamingSSEListener(
            session_id, on_delta=send_delta, on_complete=send_complete,
            on_error=send_error, timeout=600
        )
        listener.on_confirmation = lambda cid, ctype, desc, tool: send_confirmation(cid, ctype, desc, tool)
        sse_thread = threading.Thread(target=listener.listen)
        sse_thread.daemon = True
        sse_thread.start()
        time.sleep(0.5)

        success = send_prompt_async(session_id, messages, DEFAULT_DIRECTORY,
                                     provider_id, model_id, tools=tools,
                                     session_reused=session_reused)
        if not success:
            send_error({"message": "Failed to send prompt to super-agent"})
            return

        sse_thread.join(timeout=600)
        if not listener.completed and not has_error:
            send_error({"message": "Response timeout"})

    def _handle_non_streaming_logged(self, session_id, messages, request_id, created,
                                      provider_id, model_id, log_entry, start_time,
                                      tools=None, session_reused=False):
        """非流式响应（带日志），支持 tool call 解析"""
        model_name = f"{provider_id}/{model_id}"
        confirmations = []
        listener = StreamingSSEListener(
            session_id, on_delta=lambda t: None, on_complete=lambda: None,
            on_error=lambda e: None, timeout=600
        )
        listener.on_confirmation = lambda cid, ctype, desc, tool: confirmations.append(
            {"id": cid, "type": ctype, "description": desc, "tool": tool, "session_id": session_id}
        )
        sse_thread = threading.Thread(target=listener.listen)
        sse_thread.daemon = True
        sse_thread.start()
        time.sleep(0.5)

        success = send_prompt_async(session_id, messages, DEFAULT_DIRECTORY,
                                     provider_id, model_id, tools=tools,
                                     session_reused=session_reused)
        if not success:
            self._send_json(500, {"error": {"message": "Failed to send prompt", "type": "server_error"}})
            log_entry["status"] = "error"
            log_entry["error"] = "Failed to send prompt"
            log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
            update_stats(model=model_name, error=True)
            log_request_to_disk(log_entry)
            return

        sse_thread.join(timeout=600)
        text = listener.get_full_text()
        error = listener.error
        tokens = listener.tokens

        # 若期间出现了待确认请求（权限/问题），说明会话在等待用户确认，不算超时错误
        if confirmations:
            conf = confirmations[0]
            self._send_json(200, {
                "id": request_id, "object": "chat.completion",
                "created": created, "model": model_name,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text or ""},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "confirmation": conf,
            })
            log_entry["status"] = "waiting_confirmation"
            log_entry["error"] = f"等待用户确认: {conf.get('description', '')[:80]}"
            log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
            update_stats(model=model_name, streaming=False)
            log_request_to_disk(log_entry)
            return

        if error:
            err_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            self._send_json(500, {"error": {"message": err_msg, "type": "server_error"}})
            log_entry["status"] = "error"
            log_entry["error"] = err_msg
            log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
            update_stats(model=model_name, error=True)
            log_request_to_disk(log_entry)
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
                log_request_to_disk(log_entry)
                return
            if not text:
                self._send_json(500, {"error": {"message": "Empty response", "type": "server_error"}})
                log_entry["status"] = "error"
                log_entry["error"] = "Empty response"
                log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
                update_stats(model=model_name, error=True)
                log_request_to_disk(log_entry)
                return

        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if tokens:
            usage = {
                "prompt_tokens": tokens.get("input", 0),
                "completion_tokens": tokens.get("output", 0),
                "total_tokens": tokens.get("total", tokens.get("input", 0) + tokens.get("output", 0)),
            }

        # 解析 tool calls（如果 tools 参数存在）
        clean_text, tool_calls = text, []
        if tools:
            clean_text, tool_calls = _parse_tool_calls_from_text(text)

        if tool_calls:
            # 返回 tool_calls 格式（保留 tool_call 前的对话文本）
            response = {
                "id": request_id, "object": "chat.completion",
                "created": created, "model": model_name,
                "choices": [{"index": 0, "message": {
                    "role": "assistant",
                    "content": clean_text if clean_text and clean_text.strip() else None,
                    "tool_calls": tool_calls
                }, "finish_reason": "tool_calls"}],
                "usage": usage,
            }
            self._send_json(200, response)
            log_entry["status"] = "success"
            log_entry["response_preview"] = f"[tool_calls: {len(tool_calls)}]"
        else:
            response = {
                "id": request_id, "object": "chat.completion",
                "created": created, "model": model_name,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": clean_text}, "finish_reason": "stop"}],
                "usage": usage,
            }
            self._send_json(200, response)
            # 更新日志
            log_entry["status"] = "success"
            log_entry["response_preview"] = clean_text[:100]
            log_entry["duration_ms"] = int((time.time() - start_time) * 1000)
            log_entry["tokens"] = tokens
            update_stats(model=model_name, streaming=False, tokens=tokens)
            log_request_to_disk(log_entry)


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
      <h3><span class="num">6</span>多轮对话与会话复用</h3>
      <p>默认每次请求自动创建新的 super-agent 会话（无上下文记忆）。<b>带 <code>session_title</code> 参数时按标题复用</b>：同名会话已存在则复用同一会话，上下文自动延续，一句话不再开一个会话。多轮对话示例：</p>
      <div class="code-block" id="curl-multi">curl http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "NewApi/chat-pro",
    "session_title": "企微|私聊|wo-xxxxxxxx",
    "messages": [
      {"role": "user", "content": "我叫小明"},
      {"role": "assistant", "content": "你好小明！"},
      {"role": "user", "content": "我叫什么名字？"}
    ]
  }'<button class="copy-btn" onclick="copyCode('curl-multi')">复制</button></div>
      <div class="tip">同一 <code>session_title</code> 的后续请求自动复用同一会话，AI 保留上下文；不同标题之间完全隔离。不传标题则每次新建（兼容旧行为）。</div>
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
        <li><b>请求延迟</b>：首次请求需创建会话 + 等待 SSE 事件，通常 3-6 秒；复用会话的请求略快</li>
        <li><b>会话复用</b>：带 <code>session_title</code> 时按标题复用（企微/QQ 机器人固定标题，同一用户/同一群一个会话）；不传则每次新建</li>
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
      <div class="status-row"><span class="key">会话创建方式</span><span class="val">按 session_title 复用，无标题则新建</span></div>`;

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
    let html = '<table><thead><tr><th>时间</th><th>来源</th><th>调用进程</th><th>模型</th><th>类型</th><th>状态</th>'
      + '<th>Prompt</th><th>响应</th><th>耗时</th><th>Token</th></tr></thead><tbody>';
    for (const l of logs) {
      const time = new Date(l.timestamp * 1000).toLocaleTimeString();
      const tagClass = l.status === 'success' ? 'green' : l.status === 'error' ? 'red' : 'yellow';
      const streamTag = l.stream ? '<span class="tag blue">流式</span>' : '<span class="tag gray">非流式</span>';
      const tokens = l.tokens
        ? `${(l.tokens.input||0)+'+'}${(l.tokens.output||0)}=${(l.tokens.total||0)}`
        : '—';
      // 来源标签颜色
      const srcColors = {'企微':'green','QQ':'blue','密信':'blue','子智能体':'yellow','脚本':'gray','curl':'gray','Node':'gray','外部':'red'};
      const srcColor = srcColors[l.source] || 'gray';
      const srcTitle = l.source_detail ? esc(l.source_detail) : esc(l.source||'');
      const ipInfo = l.client_ip ? ` · ${esc(l.client_ip)}` : '';
      // 调用进程信息
      const callerName = l.caller || '';
      const callerPid = l.caller_pid || '';
      const callerCmd = l.caller_cmd || '';
      const callerPidStr = callerPid && callerPid !== 0 ? ` #${callerPid}` : '';
      // caller 颜色映射
      const callerColors = {
        'TeleAgent主程序':'blue', 'wecom-bot':'green', 'QQ适配器':'green',
        '量子密信适配器':'green', 'AI工厂':'blue', 'Reachy Mini':'blue',
        '机器人视觉':'blue', '面板测试':'yellow', '8088代理自身':'gray',
        'curl':'gray', 'Node服务':'gray', 'Python脚本':'gray'
      };
      const callerColor = callerColors[callerName] || 'gray';
      const callerTitle = callerCmd ? esc(callerCmd) : esc(callerName);
      const callerTag = callerName
        ? `<span class="tag ${callerColor}" title="${callerTitle}">${esc(callerName)}${callerPidStr}</span>`
        : '<span style="color:var(--dim)">—</span>';
      html += `<tr>
        <td style="white-space:nowrap">${time}</td>
        <td><span class="tag ${srcColor}" title="${srcTitle}${ipInfo}">${esc(l.source||'—')}</span></td>
        <td>${callerTag}</td>
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

    # 启动常驻全局确认监听器（支持多轮确认：后续轮次的 permission.asked 也能被捕获登记）
    start_global_confirmation_listener()

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

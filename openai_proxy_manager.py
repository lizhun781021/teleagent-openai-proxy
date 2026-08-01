#!/usr/bin/env python3
"""
TeleAgent OpenAI 代理管理工具
================================

用法:
  python openai_proxy_manager.py <command> [options]

命令:
  start     启动代理服务
  stop      停止代理服务
  restart   重启代理服务
  status    查看服务状态
  logs      查看最近日志 (默认 30 行)
  test      发送测试请求
  info      显示代理详细信息（地址、模型数、控制台链接等）
"""

import subprocess
import sys
import os
import json
import time
import urllib.request
import urllib.error

PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/com.lizhun.openai-proxy.plist")
SERVICE_NAME = "com.lizhun.openai-proxy"
PROXY_URL = "http://127.0.0.1:8088"
LOG_FILE = "/tmp/openai-proxy.log"
SCRIPT_PATH = os.path.expanduser("~/Desktop/星小辰工作空间/openai-proxy/openai_proxy.py")

# 颜色输出
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def run(cmd, check=True):
    """执行命令并返回输出"""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0 and result.stderr:
        print(f"{RED}错误: {result.stderr.strip()}{RESET}")
    return result


def is_running():
    """检查 launchd 服务是否已加载"""
    result = run("launchctl list | grep openai-proxy", check=False)
    return result.stdout.strip() != ""


def is_port_listening():
    """检查代理端口是否在监听"""
    try:
        req = urllib.request.Request(f"{PROXY_URL}/api/status", method="GET")
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


def get_status_data():
    """从代理 API 获取状态数据"""
    try:
        req = urllib.request.Request(f"{PROXY_URL}/api/status")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def cmd_start():
    """启动代理服务"""
    if is_running():
        print(f"{YELLOW}代理服务已在运行中{RESET}")
        _print_status()
        return

    if not os.path.exists(PLIST_PATH):
        print(f"{RED}未找到 launchd 配置文件: {PLIST_PATH}{RESET}")
        print(f"{DIM}请先创建配置文件{RESET}")
        sys.exit(1)

    result = run(f"launchctl load {PLIST_PATH}")
    if result.returncode == 0:
        # 等待服务启动
        for i in range(10):
            time.sleep(0.5)
            if is_port_listening():
                print(f"{GREEN}代理服务已启动{RESET}")
                _print_status()
                return
        print(f"{YELLOW}launchd 已加载，但端口未就绪，请检查日志: tail -f {LOG_FILE}{RESET}")
    else:
        print(f"{RED}启动失败{RESET}")


def cmd_stop():
    """停止代理服务"""
    if not is_running():
        print(f"{YELLOW}代理服务未在运行{RESET}")
        return

    result = run(f"launchctl unload {PLIST_PATH}")
    if result.returncode == 0:
        print(f"{GREEN}代理服务已停止{RESET}")
    else:
        print(f"{RED}停止失败{RESET}")


def cmd_restart():
    """重启代理服务"""
    if is_running():
        run(f"launchctl unload {PLIST_PATH}", check=False)
        time.sleep(1)

    if not os.path.exists(PLIST_PATH):
        print(f"{RED}未找到 launchd 配置文件: {PLIST_PATH}{RESET}")
        sys.exit(1)

    result = run(f"launchctl load {PLIST_PATH}")
    if result.returncode == 0:
        for i in range(10):
            time.sleep(0.5)
            if is_port_listening():
                print(f"{GREEN}代理服务已重启{RESET}")
                _print_status()
                return
        print(f"{YELLOW}launchd 已加载，但端口未就绪，请检查日志{RESET}")
    else:
        print(f"{RED}重启失败{RESET}")


def cmd_status():
    """查看服务状态"""
    _print_status()


def _print_status():
    """打印详细状态信息"""
    data = get_status_data()

    if not data:
        running = is_running()
        if running:
            print(f"{YELLOW}服务状态: launchd 已加载，但 API 未响应{RESET}")
        else:
            print(f"{RED}服务状态: 未运行{RESET}")
            print(f"{DIM}  启动命令: python {os.path.basename(__file__)} start{RESET}")
        return

    p = data.get("proxy", {})
    sa = data.get("super_agent", {})
    s = data.get("stats", {})

    print(f"\n{BOLD}{CYAN}════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{GREEN}  TeleAgent OpenAI 代理服务{RESET}")
    print(f"{CYAN}════════════════════════════════════════════{RESET}\n")

    # 服务状态
    status_icon = f"{GREEN}●{RESET}" if p.get("status") == "running" else f"{RED}●{RESET}"
    print(f"  {status_icon} 服务状态     {p.get('status', 'unknown')}")
    print(f"  ⏱  运行时间     {p.get('uptime_human', '-')}")
    print(f"  📡 监听地址     {p.get('listen', '-')}")
    print(f"  🤖 默认模型     {p.get('default_model', '-')}")
    print(f"  📂 工作目录     {p.get('default_directory', '-')}")

    # Super-Agent 状态
    sa_icon = f"{GREEN}●{RESET}" if sa.get("status") else f"{RED}●{RESET}"
    print(f"  {sa_icon} Super-Agent  {sa.get('url', '-')} (v{sa.get('version', '?')})")
    key_icon = f"{GREEN}●{RESET}" if sa.get("session_key_cached") else f"{RED}●{RESET}"
    print(f"  {key_icon} Session Key  {'已缓存' if sa.get('session_key_cached') else '未获取'}")

    # 统计
    print(f"\n  {BOLD}📊 统计{RESET}")
    print(f"  {'总请求':<12} {s.get('total_requests', 0)}")
    print(f"  {'流式请求':<12} {s.get('streaming_requests', 0)}")
    print(f"  {'非流式请求':<12} {s.get('non_streaming_requests', 0)}")
    print(f"  {'错误请求':<12} {s.get('error_requests', 0)}")
    print(f"  {'总 Token':<12} {s.get('total_tokens', 0):,}")

    # 模型使用
    mu = s.get("model_usage", {})
    if mu:
        print(f"\n  {BOLD}🔧 模型调用{RESET}")
        for model, count in sorted(mu.items(), key=lambda x: -x[1]):
            print(f"  {model:<30} {count} 次")

    print(f"\n  {DIM}控制台: http://127.0.0.1:8088/console{RESET}")
    print(f"  {DIM}API:    http://127.0.0.1:8088/v1/chat/completions{RESET}")
    print()


def cmd_logs():
    """查看日志"""
    lines = 30
    if len(sys.argv) > 2:
        try:
            lines = int(sys.argv[2])
        except ValueError:
            pass

    if not os.path.exists(LOG_FILE):
        print(f"{YELLOW}日志文件不存在: {LOG_FILE}{RESET}")
        print(f"{DIM}服务可能尚未启动或未产生日志{RESET}")
        return

    result = run(f"tail -{lines} {LOG_FILE}", check=False)
    if result.stdout:
        print(result.stdout)
    else:
        print(f"{YELLOW}日志为空{RESET}")


def cmd_test():
    """发送测试请求"""
    prompt = "你好，用一句话介绍你自己"
    model = "NewApi/chat-pro"
    stream = False

    # 解析参数
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        elif args[i] == "--stream":
            stream = True
            i += 1
        elif not args[i].startswith("-"):
            prompt = args[i]
            i += 1
        else:
            i += 1

    if not is_port_listening():
        print(f"{RED}代理服务未运行，请先启动: python {os.path.basename(__file__)} start{RESET}")
        sys.exit(1)

    print(f"{CYAN}模型: {model}  |  流式: {stream}  |  提问: {prompt}{RESET}\n")

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream
    }).encode()

    req = urllib.request.Request(
        f"{PROXY_URL}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        start = time.time()
        with urllib.request.urlopen(req, timeout=60) as resp:
            if stream:
                print(f"{DIM}--- 流式响应 ---{RESET}")
                for line in resp:
                    line = line.decode().strip()
                    if not line or line.startswith(":"):
                        continue
                    if line == "data: [DONE]":
                        break
                    if line.startswith("data: "):
                        chunk = json.loads(line[6:])
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            print(content, end="", flush=True)
                print()
            else:
                data = json.loads(resp.read().decode())
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                elapsed = time.time() - start
                print(f"{GREEN}{content}{RESET}")
                print(f"\n{DIM}--- 耗时 {elapsed:.2f}s | "
                      f"输入 {usage.get('prompt_tokens', 0)} + "
                      f"输出 {usage.get('completion_tokens', 0)} = "
                      f"总计 {usage.get('total_tokens', 0)} tokens ---{RESET}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"{RED}HTTP {e.code}: {body}{RESET}")
    except Exception as e:
        print(f"{RED}请求失败: {e}{RESET}")


def cmd_info():
    """显示代理详细信息"""
    print(f"\n{BOLD}{CYAN}════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  TeleAgent OpenAI 代理 - 详细信息{RESET}")
    print(f"{CYAN}════════════════════════════════════════════{RESET}\n")

    print(f"{BOLD}📁 文件位置{RESET}")
    print(f"  代理脚本:   {SCRIPT_PATH}")
    print(f"  launchd:    {PLIST_PATH}")
    print(f"  日志文件:   {LOG_FILE}")
    print()

    print(f"{BOLD}🌐 访问地址{RESET}")
    print(f"  API Base:   {PROXY_URL}/v1")
    print(f"  控制台:     {PROXY_URL}/console")
    print(f"  健康检查:   {PROXY_URL}/health")
    print()

    print(f"{BOLD}📡 API 端点{RESET}")
    endpoints = [
        ("POST", "/v1/chat/completions", "聊天补全 (OpenAI 兼容)"),
        ("GET",  "/v1/models",          "模型列表 (OpenAI 兼容)"),
        ("GET",  "/api/status",         "服务状态"),
        ("GET",  "/api/logs",           "请求日志"),
        ("GET",  "/api/sessions",       "会话列表"),
        ("POST", "/api/test",           "在线测试"),
    ]
    for method, path, desc in endpoints:
        color = GREEN if method == "GET" else YELLOW
        print(f"  {color}{method:<6}{RESET} {path:<28} {DIM}{desc}{RESET}")
    print()

    print(f"{BOLD}🔧 接入方式{RESET}")
    print(f"  API Base URL:  {PROXY_URL}/v1")
    print(f"  API Key:       any (随意填写)")
    print(f"  默认模型:      NewApi/chat-pro")
    print()

    print(f"{BOLD}⌨️  常用命令{RESET}")
    print(f"  {DIM}启动:{RESET}  python openai_proxy_manager.py start")
    print(f"  {DIM}停止:{RESET}  python openai_proxy_manager.py stop")
    print(f"  {DIM}重启:{RESET}  python openai_proxy_manager.py restart")
    print(f"  {DIM}状态:{RESET}  python openai_proxy_manager.py status")
    print(f"  {DIM}日志:{RESET}  python openai_proxy_manager.py logs [行数]")
    print(f"  {DIM}测试:{RESET}  python openai_proxy_manager.py test [提问] [--model 模型] [--stream]")
    print(f"  {DIM}信息:{RESET}  python openai_proxy_manager.py info")
    print()


def main():
    if len(sys.argv) < 2:
        print(f"\n{BOLD}TeleAgent OpenAI 代理管理工具{RESET}\n")
        print(f"{BOLD}用法:{RESET} python openai_proxy_manager.py <command> [options]\n")
        print(f"{BOLD}命令:{RESET}")
        print(f"  {GREEN}start{RESET}     启动代理服务")
        print(f"  {RED}stop{RESET}      停止代理服务")
        print(f"  {YELLOW}restart{RESET}   重启代理服务")
        print(f"  {BLUE}status{RESET}    查看服务状态")
        print(f"  {CYAN}logs{RESET}      查看最近日志 (默认 30 行)")
        print(f"  {CYAN}test{RESET}      发送测试请求")
        print(f"  {CYAN}info{RESET}      显示代理详细信息")
        print(f"\n{BOLD}示例:{RESET}")
        print(f"  python openai_proxy_manager.py status")
        print(f"  python openai_proxy_manager.py test 你好 --model NewApi/chat-pro")
        print(f"  python openai_proxy_manager.py test 讲个笑话 --stream")
        print(f"  python openai_proxy_manager.py logs 50")
        print()
        sys.exit(0)

    cmd = sys.argv[1].lower()
    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "logs": cmd_logs,
        "test": cmd_test,
        "info": cmd_info,
    }

    if cmd not in commands:
        print(f"{RED}未知命令: {cmd}{RESET}")
        print(f"{DIM}可用命令: {', '.join(commands.keys())}{RESET}")
        sys.exit(1)

    commands[cmd]()


if __name__ == "__main__":
    main()

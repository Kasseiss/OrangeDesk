"""Qt-free command line entry point for OrangeDesk."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from .client import ServiceError, request
from .engine import APP_VERSION, close_installed_ui, gui_command, hidden_spawn


def _print_status(value: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    state = {"stopped": "已停止", "starting": "正在启动", "running": "运行中", "stopping": "正在停止", "error": "需要处理"}.get(value.get("state"), "未知")
    print(f"OrangeDesk {value.get('version', APP_VERSION)} · {state}")
    print(value.get("message", ""))
    if value.get("state") == "running":
        print(f"快橙：{'已连接' if value.get('connected') else '等待连接或状态确认'} | 保活：{'暂停' if value.get('paused') else '开启'}")
        print(f"国内直连 {value.get('direct', 0)} · VPN {value.get('vpn', 0)} · 活跃 {value.get('active', 0)} · 错误 {value.get('errors', 0)}")
    if value.get("state") == "error" and value.get("session_path"):
        print("本次日志：" + value["session_path"])


def _print_whitelist(value: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if value.get("message"):
        print(value["message"])
    rules = value.get("rules", [])
    print(f"分流白名单 · {len(rules)} 条规则")
    for rule in rules:
        route = "VPN" if rule["mode"] == "vpn" else "直连"
        scope = "包含子域名" if rule["subdomains"] else "仅此域名"
        print(f"{rule['host']} · {route} · {scope}")
    if not rules:
        print("当前没有白名单规则。")
    if value.get("active"):
        print("分流运行中；规则变更约 1 秒后用于新连接，现有连接保持原路线。")
    else:
        print("分流未运行；已保存的规则将在下次启动分流时生效。")


def cli_main(argv: list[str] | None = None) -> int:
    # Stable Unicode output also when PowerShell captures/pipes this command.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="orange", description="快橙保活与临时分流，支持界面和命令行。")
    commands = parser.add_subparsers(dest="command")
    ui_parser = commands.add_parser("ui", help="打开 Qt 界面")
    ui_parser.add_argument("--whitelist", action="store_true", help="直接打开白名单编辑器")
    start = commands.add_parser("start", help="打开快橙并启动保活和分流")
    start.add_argument("--paused", action="store_true", help="启动分流，暂不自动重连")
    commands.add_parser("stop", help="停止本程序的保活与分流，恢复启动前的代理设置")
    shutdown = commands.add_parser("shutdown", help="正常停止并退出本地后台，供更新或卸载使用")
    shutdown.add_argument("--close-ui", action="store_true", help="先正常关闭本安装的 Qt 界面，确认退出后再停止后台")
    status = commands.add_parser("status", help="查看运行状态")
    status.add_argument("--json", action="store_true", help="输出 JSON")
    commands.add_parser("pause", help="暂停自动重连，继续分流")
    commands.add_parser("resume", help="恢复自动重连")
    commands.add_parser("open-vpn", help="打开已安装的快橙")
    commands.add_parser("install-vpn", help="运行随附的快橙官方安装程序")
    commands.add_parser("restore", help="恢复本工具异常退出后遗留的代理设置")
    logs = commands.add_parser("logs", help="查看本次运行日志")
    logs.add_argument("--lines", type=int, default=40)
    whitelist = commands.add_parser(
        "whitelist", help="添加、删除或查看域名分流白名单",
        description="网址或域名均可添加，默认直连并包含子域名。",
        epilog="示例：orange whitelist add https://example.com/path；orange whitelist add example.com --mode vpn --exact；orange whitelist list --json",
    )
    whitelist_commands = whitelist.add_subparsers(dest="whitelist_command", required=True)
    whitelist_add = whitelist_commands.add_parser("add", help="添加网址或域名规则")
    whitelist_add.add_argument("targets", nargs="+", metavar="target", help="网址或域名，可一次添加多个")
    whitelist_add.add_argument("--mode", choices=("direct", "vpn"), default="direct", help="分流路线，默认 direct（直连）")
    whitelist_add.add_argument("--exact", action="store_true", help="仅匹配此域名，默认包含子域名")
    whitelist_remove = whitelist_commands.add_parser("remove", help="删除指定域名规则")
    whitelist_remove.add_argument("target", help="要删除的域名或网址")
    whitelist_remove.add_argument("--exact", action="store_true", help="删除仅匹配此域名的规则，默认删除包含子域名的规则")
    whitelist_list = whitelist_commands.add_parser("list", help="查看已保存的白名单")
    whitelist_list.add_argument("--json", action="store_true", help="输出 JSON")
    commands.add_parser("version", help="查看版本")
    args = parser.parse_args(argv)
    command = args.command or "ui"
    try:
        if command == "version":
            print("OrangeDesk " + APP_VERSION)
            return 0
        if command == "ui":
            options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not getattr(sys, "frozen", False):
                options["cwd"] = str(Path(__file__).resolve().parent.parent)
            hidden_spawn(gui_command("--whitelist") if getattr(args, "whitelist", False) else gui_command(), **options)
            return 0
        if command == "whitelist":
            action = args.whitelist_command
            if action == "add":
                payload = {"text": "\n".join(args.targets), "mode": args.mode, "subdomains": not args.exact}
            elif action == "remove":
                payload = {"host": args.target, "subdomains": not args.exact}
            else:
                payload = None
            result = request("whitelist-" + action, payload)
            _print_whitelist(result, bool(getattr(args, "json", False)))
            return 0
        if command == "shutdown" and args.close_ui:
            close_installed_ui()
        payload = {"paused": args.paused} if command == "start" else {"lines": args.lines} if command == "logs" else None
        result = request(command, payload)
        if command == "start":
            # This wait is console-only; GUI/status stay responsive during compilation.
            deadline = time.monotonic() + 50
            while result.get("state") == "starting" and time.monotonic() < deadline:
                time.sleep(0.5)
                result = request("status")
            if result.get("state") == "starting":
                _print_status(result)
                print("后台仍在准备，请稍后运行 orange status。", file=sys.stderr)
                return 2
        if command == "logs":
            for line in result.get("logs", []):
                print(line)
            if not result.get("logs"):
                print("当前没有本程序的运行日志。")
        else:
            _print_status(result, bool(getattr(args, "json", False)))
        return 1 if result.get("state") == "error" else 0
    except (ServiceError, OSError) as exc:
        print("未完成：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())

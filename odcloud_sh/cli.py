"""Command line interface. Credentials and subscription URLs never print."""

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from . import __version__
from .auth import login, logout, fetch_client_config, update_from_account
from .errors import ClientError
from .network import network_mode, new_session, request
from .auth import api_endpoint
from .storage import State
from .subscription import set_subscription, update_subscription


def parser():
    p = argparse.ArgumentParser(prog="proxyctl", description="0dcloud 账号、订阅与 Mihomo 命令行管理")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--state-dir", type=Path, help="状态目录（默认 ~/.local/state/0dcloud-sh）")
    p.add_argument("--core", type=Path, help="已有 Mihomo 可执行文件路径")
    sub = p.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("login", help="从环境变量读取凭据并登录已确认的 API")
    auth.add_argument("--api-url", help="账号 API 地址（必须由用户确认）")
    auth.add_argument("--login-path", help="登录路径，默认 /auth/login（网站公开表单）")
    auth.add_argument("--password-field", choices=["passwd", "password"])
    auth.add_argument("--direct", action="store_true", help="本次账号请求不使用环境代理，仍校验证书")
    sub.add_parser("logout", help="删除本地会话，不删除订阅")
    subscription = sub.add_parser("subscription", help="配置订阅地址")
    subscription.add_subparsers(dest="subscription_command", required=True).add_parser("set", help="隐藏输入订阅 URL")
    update = sub.add_parser("update", help="下载、解析并缓存订阅；不会重启运行中的代理")
    update.add_argument("--direct", action="store_true", help="本次账号和订阅请求不使用环境代理")
    source = update.add_mutually_exclusive_group()
    source.add_argument("--from-account", action="store_true", help="从已登录网站账号页发现并下载 Clash 订阅")
    source.add_argument("--from-client-api", action="store_true", help="实验性：从已确认的 /clientConfig API 获取明文配置")
    doctor = sub.add_parser("doctor", help="只读检查登录页连通性，不发送账号凭据")
    doctor.add_argument("--api-url", help="站点地址，默认使用已保存地址")
    doctor.add_argument("--direct", action="store_true", help="不使用环境代理，仍校验证书")
    core = sub.add_parser("core", help="安装官方 Mihomo")
    install = core.add_subparsers(dest="core_command", required=True).add_parser("install")
    install.add_argument("--url", help="指定核心下载地址（不含凭据）")
    install.add_argument("--version", default="latest")
    install.add_argument("--sha256", help="校验下载文件的 SHA256")
    install.add_argument("--direct", action="store_true", help="本次核心版本查询和下载不使用环境代理")
    start = sub.add_parser("start", help="校验配置并后台运行 Mihomo")
    start.add_argument("--port", type=int, default=7890, help="本机 HTTP/SOCKS 混合代理端口")
    start.add_argument("--controller-port", type=int, help="本机控制端口，默认自动分配")
    sub.add_parser("stop", help="停止本工具启动的核心")
    sub.add_parser("status", help="查看运行状态")
    sub.add_parser("nodes", help="查看节点和代理组")
    use = sub.add_parser("use", help="切换代理组选择")
    use.add_argument("node")
    use.add_argument("--group", default="PROXY")
    env = sub.add_parser("env", help="输出可 eval 的终端代理变量")
    env.add_argument("--shell", choices=["sh", "fish"], default="sh")
    env.add_argument("--unset", action="store_true", help="清除代理环境变量（不还原此前值）")
    return p


def run(args):
    if args.command == "env" and args.unset:
        names = "http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY"
        if args.shell == "fish":
            print("\n".join(f"set -e {name};" for name in names.split()))
        else:
            print("unset " + names + ";")
        return
    state = State(args.state_dir)
    if args.command == "login":
        login(state, api_url=args.api_url, login_path=args.login_path, password_field=args.password_field)
        print("登录成功，会话已保存；密码未写入磁盘。")
    elif args.command == "logout":
        logout(state)
        print("本地会话已删除。")
    elif args.command == "doctor":
        base = args.api_url or state.read().get("api_url")
        if not base:
            raise ClientError("请用 doctor --api-url 指定要检查的站点。")
        with new_session() as session:
            request(session, "GET", api_endpoint(base, "/auth/login"), headers={"Accept": "text/html"})
        print("登录页连接成功，TLS 校验通过；未发送账号凭据。")
    elif args.command == "subscription":
        # Hidden input keeps token URLs out of argv, shell history and output.
        url = os.environ.get("_0dcloud_sub") or getpass.getpass("订阅地址（隐藏输入）: ")
        set_subscription(state, url.strip())
        print("订阅地址已保存。")
    elif args.command == "update":
        if args.from_account:
            result = update_from_account(state)
        elif args.from_client_api:
            result = fetch_client_config(state)
        else:
            result = update_subscription(state)
        print(f"配置已更新：{result['nodes']} 个节点，{result['providers']} 个节点提供者。")
    else:
        from .core import CoreManager
        core = CoreManager(state.directory, args.core)
        if args.command == "core":
            result = core.install(url=args.url, version=args.version, sha256=args.sha256)
        elif args.command == "start":
            result = core.start(state.path("subscription.yaml"), port=args.port, controller_port=args.controller_port)
        elif args.command == "stop":
            result = core.stop()
        elif args.command == "status":
            result = core.status()
        elif args.command == "nodes":
            result = core.nodes()
        elif args.command == "use":
            result = core.use(args.group, args.node)
        elif args.command == "env":
            print(core.env(shell=args.shell))
            return
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        with network_mode(direct=getattr(args, "direct", False)):
            run(args)
    except ClientError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("操作已取消。", file=sys.stderr)
        return 130
    except (OSError, ValueError):
        print("操作失败：请检查文件权限、配置和运行环境。", file=sys.stderr)
        return 1
    return 0


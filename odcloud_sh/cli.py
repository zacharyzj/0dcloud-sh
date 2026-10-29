"""Command line interface. Credentials and subscription URLs never print."""

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from . import __version__
from .auth import login, logout, update_from_account
from .client_api import discover_client_api, login_client, fetch_client_config
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
    auth.add_argument("--client-api", action="store_true", help="使用 GUI 原生 /authorization 协议；需显式指定 API")
    auth.add_argument("--allow-http", action="store_true", help="仅客户端 API：明确允许用 HTTP 明文传输账号密码")
    sub.add_parser("logout", help="删除本地会话，不删除订阅")
    subscription = sub.add_parser("subscription", help="配置订阅地址")
    subscription.add_subparsers(dest="subscription_command", required=True).add_parser("set", help="隐藏输入订阅 URL")
    update = sub.add_parser("update", help="下载、解析并缓存订阅；不会重启运行中的代理")
    update.add_argument("--direct", action="store_true", help="本次账号和订阅请求不使用环境代理")
    source = update.add_mutually_exclusive_group()
    source.add_argument("--from-account", action="store_true", help="从已登录网站账号页发现并下载 Clash 订阅")
    source.add_argument("--from-client-api", action="store_true", help="使用 GUI 签名和 AES-GCM 协议刷新，需账号环境变量")
    update.add_argument("--allow-http", action="store_true", help="仅客户端 API：明确允许用 HTTP 明文传输账号密码")
    discovery = sub.add_parser("discover", help="读取 GUI 公开发现配置，显示 API 地址；不发送账号或自动登录")
    discovery.add_argument("--direct", action="store_true", help="本次公开发现请求不使用环境代理")
    doctor = sub.add_parser("doctor", help="只读检查登录页连通性，不发送账号凭据")
    doctor.add_argument("--api-url", help="站点地址，默认使用已保存地址")
    doctor.add_argument("--direct", action="store_true", help="不使用环境代理，仍校验证书")
    core = sub.add_parser("core", help="安装官方 Mihomo")
    core_commands = core.add_subparsers(dest="core_command", required=True)
    install = core_commands.add_parser("install")
    install.add_argument("--url", help="指定核心下载地址（不含凭据）")
    install.add_argument("--version", default="latest")
    install.add_argument("--sha256", help="校验下载文件的 SHA256")
    install.add_argument("--direct", action="store_true", help="本次核心版本查询和下载不使用环境代理")
    geodata = core_commands.add_parser("geodata", help="从官方 MetaCubeX CDN 安装当前配置需要的地理规则数据库")
    geodata.add_argument("--direct", action="store_true", help="本次数据库下载不使用环境代理")
    start = sub.add_parser("start", help="校验配置并后台运行 Mihomo")
    start.add_argument("--port", type=int, default=7890, help="本机 HTTP/SOCKS 混合代理端口")
    start.add_argument("--controller-port", type=int, help="本机控制端口，默认自动分配")
    connect = sub.add_parser("connect", help="启动代理并恢复上次节点；无有效选择时自动选择代理节点")
    connect.add_argument("--port", type=int, default=7890, help="未运行时使用的本机代理端口")
    connect.add_argument("--controller-port", type=int, help="未运行时使用的本机控制端口")
    connect.add_argument("--group", help="代理组；默认按当前模式和配置自动识别")
    autostart = sub.add_parser("autostart", help="管理本机 systemd 开机自启动（启用/禁用需 root）")
    autostart_commands = autostart.add_subparsers(dest="autostart_command", required=True)
    enable = autostart_commands.add_parser("enable", help="登记开机启动，保留当前运行的代理")
    enable.add_argument("--port", type=int, default=7890)
    enable.add_argument("--controller-port", type=int)
    enable.add_argument("--group")
    autostart_commands.add_parser("disable", help="取消开机启动，保留当前运行的代理")
    autostart_commands.add_parser("status", help="查看开机启动状态")
    sub.add_parser("stop", help="停止本工具启动的核心")
    sub.add_parser("status", help="查看运行状态")
    sub.add_parser("nodes", help="查看节点和代理组")
    mode = sub.add_parser("mode", help="查看或在线切换规则/全局/直连模式，重启保留")
    mode.add_argument("value", nargs="?", choices=["rule", "global", "direct"])
    tun = sub.add_parser("tun", help="查看或切换 Linux TUN；开启需核心拥有 CAP_NET_ADMIN")
    tun.add_argument("value", nargs="?", choices=["on", "off"])
    dashboard = sub.add_parser("dashboard", help="启动使用 _0dcloud_pass 密码登录的 HTTP 控制面板（前台运行）")
    dashboard.add_argument("--host", default="0.0.0.0", choices=["0.0.0.0", "127.0.0.1"])
    dashboard.add_argument("--port", type=int, default=8787)
    dashboard.add_argument("--public-host", help="访问链接使用的 IPv4 地址；云服务器公网 NAT 场景可显式指定，启动后保留")
    dashboard.add_argument("--open", action="store_true", help="同时打开本机浏览器")
    dashboard.add_argument("--show-url", action="store_true", help="显示上次启动的面板访问地址")
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
        if args.client_api:
            if not args.api_url:
                raise ClientError("客户端登录请用 --api-url 明确指定目标；可先运行 discover 查看公开配置。")
            if args.login_path or args.password_field:
                raise ClientError("客户端登录使用样本固定协议，不支持网站登录字段选项。")
            login_client(state, api_url=args.api_url, allow_http=args.allow_http)
        else:
            if args.allow_http:
                raise ClientError("--allow-http 仅适用于 --client-api。")
            login(state, api_url=args.api_url, login_path=args.login_path, password_field=args.password_field)
        print("登录成功，会话已保存；密码未写入磁盘。")
    elif args.command == "logout":
        logout(state)
        print("本地会话已删除。")
    elif args.command == "discover":
        print(json.dumps(discover_client_api(), ensure_ascii=False, indent=2))
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
        if args.allow_http and not args.from_client_api:
            raise ClientError("--allow-http 仅适用于 --from-client-api。")
        if args.from_account:
            result = update_from_account(state)
        elif args.from_client_api:
            result = fetch_client_config(state, allow_http=args.allow_http)
        else:
            result = update_subscription(state)
        print(f"配置已更新：{result['nodes']} 个节点，{result['providers']} 个节点提供者。")
    else:
        from .core import CoreManager
        core = CoreManager(state.directory, args.core)
        if args.command == "core":
            if args.core_command == "geodata":
                result = core.install_geodata()
            else:
                result = core.install(url=args.url, version=args.version, sha256=args.sha256)
        elif args.command == "start":
            result = core.start(state.path("subscription.yaml"), port=args.port, controller_port=args.controller_port)
        elif args.command == "connect":
            result = core.connect(state.path("subscription.yaml"), port=args.port,
                                  controller_port=args.controller_port, group=args.group)
        elif args.command == "autostart":
            from . import autostart
            if args.autostart_command == "enable":
                result = autostart.enable(core, group=args.group, port=args.port,
                                          controller_port=args.controller_port)
            elif args.autostart_command == "disable":
                result = autostart.disable()
            else:
                result = autostart.status()
        elif args.command == "stop":
            result = core.stop()
        elif args.command == "status":
            result = core.status()
        elif args.command == "nodes":
            result = core.nodes()
        elif args.command == "mode":
            result = core.mode(args.value)
        elif args.command == "tun":
            result = core.tun(None if args.value is None else args.value == "on")
        elif args.command == "dashboard":
            from .dashboard import serve, show_url
            if args.show_url:
                print(show_url(state, public_host=args.public_host))
            else:
                serve(core, state, host=args.host, port=args.port, open_browser=args.open, public_host=args.public_host)
            return
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


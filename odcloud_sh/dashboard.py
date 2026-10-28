"""Small authenticated HTTP dashboard. Core secret stays on the server."""

import hmac
import hashlib
import ipaddress
import json
import os
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import secrets
import socket
import threading
import time
import webbrowser

from .errors import ClientError


def display_host(host, public_host=None):
    if host == "127.0.0.1":
        return host
    if public_host:
        try:
            address = ipaddress.IPv4Address(public_host)
            if address.is_unspecified or address.is_multicast or address.is_loopback:
                raise ValueError()
            return str(address)
        except ValueError:
            raise ClientError("--public-host must be a reachable IPv4 address") from None
    # UDP connect only asks the kernel which source address it would use;
    # no packet, external IP service or account data is sent.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def show_url(state, public_host=None):
    access = state.read("dashboard-access.json")
    if not access:
        raise ClientError("Dashboard has not been started; run proxyctl dashboard")
    host = access.get("host", "127.0.0.1")
    address = display_host(host, public_host) if public_host else (
        access.get("display_host") or display_host(host, access.get("public_host")))
    return f"http://{address}:{access['port']}/"


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, core, host="0.0.0.0", port=8787):
        if host not in ("0.0.0.0", "127.0.0.1") or type(port) is not int or not 1024 <= port <= 65535:
            raise ClientError("Dashboard host must be 0.0.0.0 or 127.0.0.1; port must be 1024..65535")
        password = os.environ.get("_0dcloud_pass")
        if not password:
            raise ClientError("Dashboard requires _0dcloud_pass; source ~/.zshrc and export _0dcloud_pass before starting")
        self.password_salt = secrets.token_bytes(16)
        self.password_hash = self.hash_password(password)
        self.sessions = {}
        self.login_attempts = []
        self.auth_lock = threading.Lock()
        self.core = core
        self.action_lock = threading.Lock()
        self.proxy_port = core.controls()["status"].get("proxy_port") or 7890
        self.page = files("odcloud_sh").joinpath("dashboard.html").read_bytes()
        super().__init__((host, port), DashboardHandler)

    def hash_password(self, password):
        return hashlib.scrypt(password.encode(), salt=self.password_salt, n=16384, r=8, p=1)

    def login(self, password):
        with self.auth_lock:
            now = time.monotonic()
            self.login_attempts = [stamp for stamp in self.login_attempts if now - stamp < 60]
            if len(self.login_attempts) >= 10:
                return 429, None
            self.login_attempts.append(now)
            if not hmac.compare_digest(self.hash_password(password), self.password_hash):
                return 401, None
            self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
            if len(self.sessions) >= 64:
                self.sessions.pop(next(iter(self.sessions)))
            session = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(session.encode()).digest()] = now + 12 * 3600
            return 200, session


class DashboardHandler(BaseHTTPRequestHandler):
    # Avoid logs containing browser input or authentication tokens.
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def respond(self, status, body, content_type="application/json; charset=utf-8", cookie=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        key = self.session_key()
        with self.server.auth_lock:
            expiry = self.server.sessions.get(key, 0)
            if expiry <= time.monotonic():
                self.server.sessions.pop(key, None)
        if expiry <= time.monotonic():
            self.respond(401, {"error": "请使用账号密码登录面板"})
            return False
        return self.same_origin()

    def session_key(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookies.get("odcloud_session")
            return hashlib.sha256(value.value.encode()).digest() if value else None
        except CookieError:
            return None

    def session_cookie(self, session, *, clear=False):
        cookie = f"odcloud_session={session}; Path=/; HttpOnly; SameSite=Strict"
        if clear:
            cookie += "; Max-Age=0"
        if self.headers.get("Origin", "").startswith("https://"):
            cookie += "; Secure"
        return cookie

    def same_origin(self):
        # JSON-only actions, SameSite cookies and browser origin checks prevent CSRF.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if self.headers.get("Sec-Fetch-Site") == "cross-site" or (
                origin and origin not in ("http://" + host, "https://" + host)):
            self.respond(403, {"error": "不允许跨站操作"})
            return False
        return True

    def do_GET(self):
        if self.path == "/":
            self.respond(200, self.server.page, "text/html; charset=utf-8")
            return
        if self.path != "/api/state":
            self.respond(404, {"error": "接口不存在"})
            return
        if not self.authorized():
            return
        try:
            with self.server.action_lock:
                core = self.server.core
                value = core.controls()
                # Expose only UI fields, never runtime config, core secret,
                # account cookies, subscription URL or node credentials.
                value["status"] = {k: value["status"][k] for k in ("running", "installed", "pid", "proxy_port")}
                value["proxies"] = {}
                if value["status"]["running"]:
                    for name, node in core.nodes().items():
                        value["proxies"][name] = {key: node[key] for key in
                                                  ("type", "all", "now", "alive", "history", "udp") if key in node}
            self.respond(200, value)
        except ClientError as exc:
            self.respond(409, {"error": str(exc)})
        except (OSError, ValueError, KeyError, TypeError):
            self.respond(500, {"error": "无法读取核心状态"})

    def do_POST(self):
        if self.path == "/api/login":
            if not self.same_origin():
                return
        elif not self.authorized():
            return
        if self.path not in ("/api/login", "/api/logout", "/api/use", "/api/mode", "/api/tun", "/api/start", "/api/stop", "/api/delay", "/api/connect", "/api/disconnect"):
            self.respond(404, {"error": "接口不存在"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self.respond(415, {"error": "请求必须使用 JSON"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192 or self.headers.get("Transfer-Encoding"):
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, OSError):
            self.respond(400, {"error": "无效的 JSON 请求"})
            return
        if self.path == "/api/login":
            password = data.get("password")
            if not isinstance(password, str) or not password or len(password.encode()) > 4096:
                self.respond(400, {"error": "请输入密码"})
                return
            status, session = self.server.login(password)
            if status != 200:
                self.respond(status, {"error": "登录尝试过多，请一分钟后重试" if status == 429 else "密码错误"})
            else:
                self.respond(200, {"authenticated": True}, cookie=self.session_cookie(session))
            return
        if self.path == "/api/logout":
            with self.server.auth_lock:
                self.server.sessions.pop(self.session_key(), None)
            self.respond(200, {"authenticated": False}, cookie=self.session_cookie("", clear=True))
            return
        try:
            with self.server.action_lock:
                core = self.server.core
                if self.path == "/api/use":
                    if not all(isinstance(data.get(k), str) and data[k] for k in ("group", "node")):
                        raise ClientError("请选择代理组和节点")
                    result = core.use(data["group"], data["node"])
                elif self.path == "/api/mode":
                    if data.get("mode") not in ("rule", "global", "direct"):
                        raise ClientError("模式必须为 rule、global 或 direct")
                    result = core.mode(data["mode"])
                elif self.path == "/api/tun":
                    if type(data.get("enabled")) is not bool:
                        raise ClientError("TUN 状态必须为布尔值")
                    result = core.tun(data["enabled"])
                elif self.path == "/api/delay":
                    if not isinstance(data.get("node"), str):
                        raise ClientError("请选择节点")
                    result = core.delay(data["node"])
                elif self.path in ("/api/start", "/api/connect"):
                    # Repeated clicks or another browser connecting are harmless.
                    status = core.controls()["status"]
                    if not status["running"]:
                        core.start(core.state_dir / "subscription.yaml", port=self.server.proxy_port)
                    result = {"connected": core.controls()["status"]["running"]}
                else:
                    status = core.controls()["status"]
                    if status["running"]:
                        self.server.proxy_port = status["proxy_port"]
                    core.stop()
                    result = {"connected": core.controls()["status"]["running"]}
            self.respond(200, result)
        except ClientError as exc:
            self.respond(409, {"error": str(exc)})
        except (ValueError, OSError, TypeError, KeyError):
            self.respond(500, {"error": "操作未完成，请检查配置和服务权限"})


def serve(core, state, *, host="0.0.0.0", port=8787, open_browser=False, public_host=None):
    previous = state.read("dashboard-access.json")
    public_host = public_host or previous.get("public_host")
    address = display_host(host, public_host)
    with DashboardServer(core, host, port) as server:
        state.write({"host": host, "display_host": address, "public_host": public_host,
                     "port": server.server_port}, "dashboard-access.json")
        url = show_url(state)
        print(f"Dashboard listening on {host}:{server.server_port}", flush=True)
        print(url, flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print("\nDashboard stopped; proxy core keeps its current state.", flush=True)

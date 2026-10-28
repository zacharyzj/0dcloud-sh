"""Explicit-origin account session adapter; no password persistence."""

import json
import os
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

from .errors import ClientError
from .network import new_session, request, validate_url, origin
from .subscription import decode_subscription, save_config, update_subscription


def api_endpoint(base, path):
    parts = validate_url(base)
    if parts.query:
        raise ClientError("账号 API 基础地址不能包含查询参数，请提供站点或 API 根路径。")
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise ClientError("API 路径必须是以 / 开头的相对路径。")
    target = base.rstrip("/") + path
    if origin(base) != origin(target):
        raise ClientError("API 地址与配置站点不一致。")
    return target


def json_result(payload):
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        raise ClientError("账号接口未返回 JSON；请检查 API 地址和接口路径。") from None
    if not isinstance(value, dict):
        raise ClientError("账号接口返回格式无效。")
    result = value.get("ret", value.get("success"))
    if result == 2:
        raise ClientError("账号要求二步验证码，请设置 _0dcloud_otp 后重试登录。")
    if result is not True and (isinstance(result, bool) or result != 1):
        # The remote message may echo an email, password or token.
        raise ClientError("账号接口未确认成功，请检查凭据、接口路径或服务协议。")
    return value


def _save_cookies(state, session, base):
    cookies = []
    for cookie in session.cookies:
        cookies.append({"name": cookie.name, "value": cookie.value,
                        "domain": cookie.domain, "path": cookie.path,
                        "secure": cookie.secure, "expires": cookie.expires})
    state.write({"api_url": base, "cookies": cookies}, "session.json")


def account_session(state, base):
    session = new_session()
    saved = state.read("session.json")
    if saved and origin(saved.get("api_url", "")) != origin(base):
        raise ClientError("登录会话属于其他 API 站点，请重新登录。")
    cookies = saved.get("cookies", [])
    if not isinstance(cookies, list):
        session.close()
        raise ClientError("登录会话文件损坏，请重新登录。")
    try:
        for item in cookies:
            if not isinstance(item, dict) or not all(isinstance(item.get(key), str)
                                                    for key in ("name", "value", "domain", "path")):
                raise ValueError("invalid cookie")
            session.cookies.set(item["name"], item["value"], domain=item["domain"],
                                path=item["path"], secure=bool(item.get("secure", False)),
                                expires=item.get("expires"))
    except (KeyError, TypeError, ValueError):
        session.close()
        raise ClientError("登录会话文件损坏，请重新登录。") from None
    return session


def login(state, *, api_url=None, login_path=None, password_field=None):
    email = os.environ.get("_0dcloud_mail", "")
    password = os.environ.get("_0dcloud_pass", "")
    if not email or not password:
        raise ClientError("缺少 _0dcloud_mail 或 _0dcloud_pass；请先 source ~/.zshrc，并导出变量。")
    settings = state.read()
    base = api_url or settings.get("api_url")
    if not base:
        raise ClientError("请使用 login --api-url 指定并确认实际账号 API 地址。")
    validate_url(base)
    path = login_path or settings.get("login_path", "/auth/login")
    field = password_field or settings.get("password_field", "passwd")
    if field not in ("passwd", "password"):
        raise ClientError("密码字段仅支持 passwd 或 password。")
    with new_session() as session:
        parts = validate_url(base)
        origin_header = parts.scheme + "://" + parts.netloc
        payload, _ = request(session, "POST", api_endpoint(base, path),
                             data={"email": email, field: password, "remember_me": "on",
                                   "code": os.environ.get("_0dcloud_otp", "")},
                             headers={"Referer": api_endpoint(base, "/auth/login"),
                                      "Origin": origin_header})
        result = json_result(payload)
        # A token-only protocol needs an explicit adapter. Do not report a
        # durable login if there is no session this client can replay.
        if not session.cookies:
            raise ClientError("登录响应未设置会话 Cookie；此服务可能需要签名或令牌适配，尚不能保存会话。")
        _save_cookies(state, session, base)
    settings.update({"api_url": base, "login_path": path, "password_field": field})
    state.write(settings)
    return {"authenticated": True}


def fetch_client_config(state):
    settings = state.read()
    base = settings.get("api_url")
    if not base or not state.read("session.json"):
        raise ClientError("尚未登录，请先执行 login。")
    with account_session(state, base) as session:
        payload, _ = request(session, "POST", api_endpoint(base, "/clientConfig"), data={})
        result = json_result(payload)
        body = result.get("data")
        if isinstance(body, dict) and any(k in body for k in ("ciphertext", "tag", "iv")):
            raise ClientError("服务返回加密配置，需要确认客户端签名及 AES-GCM 协议；可先使用标准订阅地址。")
        if isinstance(body, dict):
            body = body.get("yaml", body.get("config", body))
        if isinstance(body, dict):
            body = json.dumps(body)
        if not isinstance(body, str):
            raise ClientError("服务未返回可识别配置，需要适配实际客户端协议。")
        config = decode_subscription(body.encode())
        _save_cookies(state, session, base)
    path = save_config(state, config)
    return {"path": str(path), "nodes": len(config.get("proxies", [])),
            "providers": len(config.get("proxy-providers", {}))}


class _DashboardLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.login_form = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and (attrs.get("id") == "login_form"
                              or "/auth/login" in attrs.get("action", "")):
            self.login_form = True
        for name in ("href", "data-clipboard-text", "data-url", "value"):
            value = attrs.get(name)
            if value and "/link/" in value:
                self.links.append(value)


def discover_subscription(state):
    """Return one dashboard-declared Clash URL; never invent a host or token.

    This does not save a newly discovered URL: the caller commits it only
    after downloading and decoding the new subscription successfully.
    """
    settings = state.read()
    base = settings.get("api_url")
    if not base or not state.read("session.json"):
        raise ClientError("尚未登录，请先执行 login。")
    dashboard = api_endpoint(base, "/user")
    with account_session(state, base) as session:
        try:
            payload, _ = request(session, "GET", dashboard, redirects=False)
        except ClientError as exc:
            if "重定向" in str(exc):
                raise ClientError("账号页面要求重新登录，请执行 login。") from None
            raise
        try:
            page = payload.decode("utf-8-sig")
        except UnicodeError:
            raise ClientError("账号页面编码无法识别，不能提取订阅。") from None
        parsed = _DashboardLinks()
        parsed.feed(page)
        if parsed.login_form:
            raise ClientError("账号页面返回登录表单，请重新登录。")
        # Some dashboards place explicit URLs in inline import handlers.
        # Read only literal URLs; do not execute JavaScript or synthesize one.
        literals = re.findall(r"https?://[^\s\"'<>\\]+", page)
        candidates = []
        for value in parsed.links + literals:
            target = urljoin(dashboard, unescape(value).replace("\\/", "/"))
            try:
                parts = validate_url(target)
            except ClientError:
                continue
            query = parse_qs(parts.query)
            kind = query.get("clash", [])
            if not parts.path.startswith("/link/") or len(kind) != 1 or kind[0] not in ("1", "2"):
                continue
            rank = 0 if kind[0] == "2" else 1
            if (rank, target) not in candidates:
                candidates.append((rank, target))
        if not candidates:
            raise ClientError("已登录页面未提供 Clash/Mihomo 订阅链接，请手动设置订阅地址。")
        candidates.sort(key=lambda candidate: candidate[0])
        best = [url for rank, url in candidates if rank == candidates[0][0]]
        if len(best) != 1:
            raise ClientError("账号页面包含多个不同 Clash 订阅，请手动选择并设置地址。")
        _save_cookies(state, session, base)
        return best[0]


def update_from_account(state):
    return update_subscription(state, url=discover_subscription(state), persist_url=True)


def logout(state):
    path = state.path("session.json")
    path.unlink(missing_ok=True)


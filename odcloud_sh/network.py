"""Bounded HTTP, with errors that never expose subscription tokens."""

import ipaddress
import os
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urljoin, urlsplit

import requests
from requests.sessions import Session

from .errors import ClientError

MAX_RESPONSE = 16 * 1024 * 1024
_DIRECT = ContextVar("odcloud_direct", default=False)


@contextmanager
def network_mode(*, direct=False):
    """Scope direct mode to this command without changing the shell environment."""
    token = _DIRECT.set(direct)
    try:
        yield
    finally:
        _DIRECT.reset(token)


def _exception_chain(error):
    pending, seen = [error], set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        for nested in (item.__cause__, item.__context__, getattr(item, "reason", None), *item.args):
            if isinstance(nested, BaseException):
                pending.append(nested)


def _network_error(error, session, url):
    # Never render exception messages: requests embeds URLs, query tokens and
    # proxy credentials in those strings. Inspect only exception types/errno.
    causes = list(_exception_chain(error))
    classes = {type(item).__name__ for item in causes}
    is_proxy = isinstance(error, requests.exceptions.ProxyError)
    if is_proxy:
        reason = "代理连接失败（ProxyError）"
        if any(isinstance(item, socket.gaierror) for item in causes) or "NameResolutionError" in classes:
            reason += "：代理域名无法解析"
        elif any(isinstance(item, ConnectionRefusedError) for item in causes):
            reason += "：代理端口拒绝连接"
        hint = "请检查 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY；可加 --direct 单独验证直连。"
    elif isinstance(error, requests.exceptions.SSLError):
        reason = "TLS 握手或证书校验失败（SSLError）"
        hint = "请检查系统时间、CA 证书和 HTTPS 代理；企业证书可通过 REQUESTS_CA_BUNDLE 指定。"
    elif isinstance(error, requests.exceptions.Timeout):
        reason = ("建立连接超时（ConnectTimeout）" if isinstance(error, requests.exceptions.ConnectTimeout)
                  else "等待服务响应超时（ReadTimeout）")
        hint = "请检查站点和代理连通性后重试。"
    elif isinstance(error, requests.exceptions.InvalidSchema):
        reason = "代理协议不可用或缺少对应依赖（InvalidSchema）"
        hint = "SOCKS 代理需要 PySocks；可改用 HTTP 代理或加 --direct。"
    elif any(isinstance(item, socket.gaierror) for item in causes) or "NameResolutionError" in classes:
        reason = "域名解析失败（DNS）"
        hint = "请检查 DNS 和网络连接。"
    elif any(isinstance(item, ConnectionRefusedError) for item in causes):
        reason = "连接被拒绝（ConnectionRefused）"
        hint = "请检查服务或本机代理是否已经启动。"
    elif isinstance(error, requests.exceptions.ConnectionError):
        reason = "连接中断或无法建立连接（ConnectionError）"
        hint = "请检查站点和代理连通性后重试。"
    else:
        reason = "HTTP 传输失败（RequestException）"
        hint = "请检查网络和服务可达性。"
    proxies = dict(session.proxies)
    if session.trust_env:
        proxies.update(requests.utils.get_environ_proxies(url))
    mode = "使用环境或会话代理" if any(proxies.values()) else "直连"
    return ClientError(f"{reason}；当前{mode}。{hint}")


class ExplicitAuthSession(Session):
    """Honor proxy/CA environment settings without implicitly reading .netrc."""

    def prepare_request(self, request):
        # requests otherwise discovers netrc credentials for every user-supplied
        # subscription host. Explicit no-op auth disables that implicit lookup
        # without disabling corporate proxy and CA environment configuration.
        if request.auth is None:
            request.auth = _NoImplicitAuth()
        return super().prepare_request(request)


class _NoImplicitAuth(requests.auth.AuthBase):
    def __call__(self, request):
        return request


def validate_url(url):
    try:
        parts = urlsplit(url)
        port = parts.port
    except (ValueError, TypeError, AttributeError):
        raise ClientError("地址格式无效。") from None
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ClientError("地址需要主机名，且不能包含用户信息或片段。")
    local = parts.hostname.lower() == "localhost"
    try:
        local = local or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        pass
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ClientError("远程服务必须使用 HTTPS；仅本机测试允许 HTTP。")
    return parts


def origin(url):
    p = validate_url(url)
    return p.scheme, p.hostname.lower(), p.port or (443 if p.scheme == "https" else 80)


def request(session, method, url, *, data=None, headers=None, timeout=25, redirects=False):
    validate_url(url)
    method = method.upper()
    for attempt in range(6):
        try:
            with session.request(method, url, data=data, headers=headers, timeout=(10, timeout),
                                 allow_redirects=False, stream=True) as response:
                if response.is_redirect or response.is_permanent_redirect:
                    if not redirects or method != "GET":
                        raise ClientError("服务返回重定向；请配置最终 API 地址后重试。")
                    location = response.headers.get("Location")
                    if not location:
                        raise ClientError("服务返回无效重定向。")
                    target = urljoin(url, location)
                    validate_url(target)
                    # Credentials and account cookies must never cross origins.
                    if origin(target) != origin(url):
                        raise ClientError("订阅跳转到了其他站点；请核实并直接设置最终地址。")
                    url = target
                    continue
                if response.status_code in (401, 403):
                    raise ClientError("服务拒绝访问（401/403）；请检查登录状态或订阅有效期。")
                if response.status_code >= 400:
                    raise ClientError(f"服务请求失败（HTTP {response.status_code}）。")
                chunks, count = [], 0
                for chunk in response.iter_content(65536):
                    count += len(chunk)
                    if count > MAX_RESPONSE:
                        raise ClientError("服务响应超过 16 MiB，已拒绝。")
                    chunks.append(chunk)
                return b"".join(chunks), dict(response.headers)
        except requests.RequestException as exc:
            raise _network_error(exc, session, url) from None
    raise ClientError("服务重定向次数过多。")


def new_session():
    session = ExplicitAuthSession()
    if _DIRECT.get():
        session.trust_env = False
        # Disabling proxy discovery must not drop an explicit CA trust store.
        session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE") or True
    session.headers.update({"User-Agent": "0dcloud-sh/0.1 (Mihomo; Linux)", "Accept": "application/json, text/yaml, */*"})
    return session


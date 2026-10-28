"""Decode standard Mihomo YAML/JSON and common base64 URI subscriptions."""

import base64
from urllib.parse import unquote, parse_qs, urlsplit

import yaml

from .errors import ClientError
from .network import new_session, request, validate_url
from .storage import atomic_write


def b64decode(text):
    return base64.urlsafe_b64decode(text.strip() + "=" * (-len(text.strip()) % 4))


def _endpoint(parts):
    if not parts.hostname or not parts.port or not 1 <= parts.port <= 65535:
        raise ValueError("invalid endpoint")
    return parts.hostname, parts.port


def uri_node(uri, number):
    parts = urlsplit(uri)
    name = unquote(parts.fragment) or f"node-{number}"
    if parts.scheme == "ss":
        body = uri[5:].split("#", 1)[0].split("?", 1)[0]
        if "@" not in body:
            body = b64decode(body).decode("utf-8")
        credentials, endpoint = body.rsplit("@", 1)
        credentials = unquote(credentials)
        if ":" not in credentials:
            credentials = b64decode(credentials).decode("utf-8")
        method, password = credentials.split(":", 1)
        server, port = _endpoint(urlsplit("ss://" + endpoint))
        query = parse_qs(parts.query)
        if query.get("plugin"):
            raise ClientError("URI 订阅含 Shadowsocks 插件，请改用 Clash/Mihomo YAML 订阅。")
        return {"name": name, "type": "ss", "server": server, "port": port,
                "cipher": unquote(method), "password": unquote(password), "udp": True}
    if parts.scheme == "trojan":
        server, port = _endpoint(parts)
        query = parse_qs(parts.query)
        transport = query.get("type", ["tcp"])[0]
        if transport != "tcp":
            raise ClientError("此 Trojan 传输方式需要 Clash/Mihomo YAML 订阅。")
        node = {"name": name, "type": "trojan", "server": server, "port": port,
                "password": unquote(parts.username or ""), "udp": True}
        if not node["password"]:
            raise ValueError("missing password")
        if query.get("sni"):
            node["sni"] = query["sni"][0]
        if query.get("allowInsecure", ["0"])[0] == "1":
            node["skip-cert-verify"] = True
        return node
    raise ClientError("URI 订阅包含暂不支持的协议，请使用 Clash/Mihomo YAML 订阅。")


def validate_config(config):
    if not isinstance(config, dict):
        raise ClientError("订阅不是 Clash/Mihomo 配置对象。")
    proxies = config.get("proxies", [])
    providers = config.get("proxy-providers", {})
    if not isinstance(proxies, list) or not isinstance(providers, dict):
        raise ClientError("订阅中的 proxies 或 proxy-providers 类型无效。")
    if not proxies and not providers:
        raise ClientError("订阅中没有代理节点或节点提供者。")
    seen = set()
    for node in proxies:
        if not isinstance(node, dict) or not isinstance(node.get("name"), str) or not node["name"]:
            raise ClientError("订阅包含无效节点。")
        if node["name"] in seen:
            raise ClientError("订阅包含重复节点名。")
        seen.add(node["name"])
    if not config.get("proxy-groups"):
        group = {"name": "PROXY", "type": "select"}
        if proxies:
            group["proxies"] = [node["name"] for node in proxies]
        if providers:
            group["use"] = list(providers)
        config["proxy-groups"] = [group]
        if not config.get("rules"):
            config["rules"] = ["MATCH,PROXY"]
    return config


def decode_subscription(payload):
    try:
        text = payload.decode("utf-8-sig").strip()
        config = yaml.safe_load(text)
    except (UnicodeError, yaml.YAMLError):
        raise ClientError("订阅不是有效的 UTF-8 YAML/JSON 配置。") from None
    if isinstance(config, dict):
        # Accept an explicitly plain API envelope; encrypted payloads require
        # the actual service protocol and must not be guessed.
        for key in ("config", "yaml"):
            if isinstance(config.get(key), str):
                return decode_subscription(config[key].encode())
        return validate_config(config)
    if text.startswith(("<html", "<!DOCTYPE", "<!doctype")):
        raise ClientError("订阅返回 HTML 页面，请提供实际订阅链接。")
    if "://" not in text:
        try:
            text = b64decode("".join(text.split())).decode("utf-8")
        except (ValueError, UnicodeError):
            raise ClientError("订阅格式无法识别，请使用 Clash/Mihomo YAML 链接。") from None
    try:
        nodes = [uri_node(line.strip(), i + 1) for i, line in enumerate(text.splitlines()) if line.strip()]
    except (ValueError, UnicodeError):
        raise ClientError("订阅中的节点 URI 无效。") from None
    return validate_config({"proxies": nodes,
                            "proxy-groups": [{"name": "PROXY", "type": "select", "proxies": [n["name"] for n in nodes]}],
                            "rules": ["MATCH,PROXY"]})


def save_config(state, config):
    validate_config(config)
    path = state.path("subscription.yaml")
    atomic_write(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    return path


def set_subscription(state, url):
    if url.startswith("clientconfig://"):
        raise ClientError("这是原客户端的内部配置标记，需要适配 /clientConfig 签名协议；请提供网站导出的 Clash/Mihomo HTTPS 订阅链接。")
    validate_url(url)
    settings = state.read()
    settings["subscription_url"] = url
    state.write(settings)


def update_subscription(state, *, url=None, persist_url=False):
    settings = state.read()
    url = url or settings.get("subscription_url")
    if not url:
        raise ClientError("尚未设置订阅地址，请运行 proxyctl subscription set。")
    # Account cookies are deliberately not sent to subscription/CDN hosts.
    with new_session() as session:
        session.headers["User-Agent"] = "clash.meta/0dcloud-sh"
        payload, headers = request(session, "GET", url, redirects=True)
    config = decode_subscription(payload)
    path = save_config(state, config)
    if persist_url:
        set_subscription(state, url)
    return {"path": str(path), "nodes": len(config.get("proxies", [])),
            "providers": len(config.get("proxy-providers", {}))}


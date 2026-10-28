"""0dcloud 2.0.30 native client protocol, recovered from its Dart AOT snapshot.

The constants below ship in the public GUI; they are not account credentials.
Website sessions and native-client sessions remain separate. A discovery result
never authorizes sending credentials to a new server.
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import ClientError
from .network import new_session, request, validate_url
from .subscription import decode_subscription, save_config

SIGN_TEXT = "CQl9EwqeY4kK0wYVUjJsfsDgbFZOCKhZCjtQ0tFAupE"
AES_TEXT = "XE5P0YBMvqfzZLAS7C7wNZYS984tNzqzIz4yAnH_tjU"
OSS_URL = "https://songossb.oss-cn-guangzhou.aliyuncs.com/api.json"
OSS_REFERER = "https://fhskdhfkbbbdvds.cndb"
COS_URL = "https://dhsajnn-1315215305.cos.ap-shanghai.myqcloud.com/api.json"
TXT_DOMAIN = "api.apiabs.top"
PROFILE_XOR = bytes((163, 92, 225, 114, 159, 75, 216, 38, 62, 192, 87, 138, 29, 244, 107, 147))
APP_VERSION = "2.0.30"
SESSION_FILE = "client-session.json"


def client_signature(device_id, email, timestamp, nonce):
    message = "\n".join(("POST", "/clientConfig", timestamp, nonce, device_id, email))
    return hmac.new(SIGN_TEXT.encode(), message.encode(), hashlib.sha256).hexdigest()


def decode_client_response(payload):
    """Authenticate GCM before parsing or saving any plaintext."""
    result = _result(payload)
    data = result.get("data")
    if not isinstance(data, str) or not data.strip():
        raise ClientError("客户端接口未返回配置数据。")
    if result.get("enc") == "aes-256-gcm":
        try:
            raw = _base64(data)
            if len(raw) < 28:
                raise ValueError()
            plain = AESGCM(hashlib.sha256(AES_TEXT.encode()).digest()).decrypt(raw[:12], raw[12:], None)
        except (ValueError, binascii.Error, InvalidTag):
            raise ClientError("客户端 AES-GCM 配置认证失败；旧配置已保留。") from None
    elif result.get("enc") in (None, "", "plain", "base64"):
        # The GUI tries UTF-8 Base64, then treats the original data as YAML.
        try:
            decoded = _base64(data).decode("utf-8")
            plain = decoded.encode() if decoded.strip() else data.encode()
        except (ValueError, UnicodeError, binascii.Error):
            plain = data.encode()
    else:
        raise ClientError("客户端配置使用未知加密方式，请检查客户端协议版本。")
    return decode_subscription(plain)


def _base64(value):
    value = value.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)


def _result(payload):
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        raise ClientError("客户端接口未返回 JSON；请检查 API 地址。") from None
    if not isinstance(value, dict) or type(value.get("ret")) is not int or value["ret"] != 1:
        # Do not echo remote messages, which may contain credentials or URLs.
        raise ClientError("客户端接口未确认成功，请检查账号、API 地址和协议版本。")
    return value


def _base(url, allow_http):
    parts = validate_url(url, allow_http=allow_http)
    if parts.query or parts.fragment:
        raise ClientError("客户端 API 请使用不含查询参数的根地址。")
    return url.rstrip("/")


def _credentials():
    email = os.environ.get("_0dcloud_mail", "")
    password = os.environ.get("_0dcloud_pass", "")
    if not email or not password:
        raise ClientError("客户端刷新需要 _0dcloud_mail 和 _0dcloud_pass；请 source ~/.zshrc 并导出变量，密码不会保存。")
    return email, password


def _device_id(state):
    saved = state.read("client-device.json")
    device = saved.get("device_id")
    if not isinstance(device, str) or not re.fullmatch(r"[0-9a-f]{32}", device):
        device = secrets.token_hex(16)
        state.write({"device_id": device}, "client-device.json")
    return device


def _session_state(session, base, email):
    return {"api_url": base, "account_hash": hashlib.sha256(email.encode()).hexdigest(),
            "cookies": [{"name": c.name, "value": c.value, "domain": c.domain,
                         "path": c.path, "secure": c.secure, "expires": c.expires}
                        for c in session.cookies]}


def login_client(state, *, api_url, allow_http=False):
    # A caller must explicitly select the server; never use a discovery result
    # or silently reuse the website hostname for native authentication.
    base = _base(api_url, allow_http)
    email, password = _credentials()
    with new_session() as session:
        session.headers["User-Agent"] = "0dcloud/2026"
        payload, _ = request(session, "GET", base + "/authorization",
                             params={"email": email, "passwd": password}, allow_http=allow_http)
        _result(payload)
        saved = _session_state(session, base, email)
    _device_id(state)
    state.write(saved, SESSION_FILE)
    return {"authenticated": True, "protocol": "native-client"}


def fetch_client_config(state, *, allow_http=False):
    saved = state.read(SESSION_FILE)
    if not saved:
        raise ClientError("尚未登录客户端 API，请执行 login --client-api --api-url <已确认的API地址>。")
    base = _base(saved.get("api_url", ""), allow_http)
    email, password = _credentials()
    if saved.get("account_hash") != hashlib.sha256(email.encode()).hexdigest():
        raise ClientError("当前账号与客户端登录账号不同，请重新执行 login --client-api。")
    device = _device_id(state)
    timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
    headers = {"User-Agent": "0dcloud/2026", "X-Client-Id": device,
               "X-Client-Ts": timestamp, "X-Client-Nonce": nonce,
               "X-Client-Sig": client_signature(device, email, timestamp, nonce),
               "X-App-Version": APP_VERSION}
    with new_session() as session:
        cookies = saved.get("cookies", [])
        if not isinstance(cookies, list):
            raise ClientError("客户端会话损坏，请重新登录。")
        try:
            for c in cookies:
                session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"],
                                    secure=c["secure"], expires=c.get("expires"))
        except (TypeError, KeyError, ValueError):
            raise ClientError("客户端会话损坏，请重新登录。") from None
        payload, _ = request(session, "POST", base + "/clientConfig",
                             data={"email": email, "passwd": password, "platform": "linux"},
                             headers=headers, allow_http=allow_http)
        config = decode_client_response(payload)
        current = _session_state(session, base, email)
    path = save_config(state, config)
    state.write(current, SESSION_FILE)
    return {"path": str(path), "nodes": len(config.get("proxies", [])),
            "providers": len(config.get("proxy-providers", {}))}


def _discovered_urls(payload):
    try:
        config = json.loads(payload)
    except (ValueError, UnicodeError):
        raise ClientError("客户端发现配置不是 JSON。") from None
    if not isinstance(config, dict):
        raise ClientError("客户端发现配置格式无效。")
    def strings(value, depth=0):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list) and depth < 32:
            for item in value:
                yield from strings(item, depth + 1)
    candidates = []
    for key in ("api_url", "api_url_fallback", "api_urls"):
        candidates.extend(strings(config.get(key)))
    urls = []
    for url in candidates:
        if "你的" in url:
            continue
        try:
            candidate = _base(url.strip(), True)
        except ClientError:
            continue
        if candidate not in urls:
            urls.append(candidate)
    if not urls:
        raise ClientError("发现配置没有有效的客户端 API 地址。")
    return urls


def _txt_urls(response):
    if not isinstance(response, dict) or not isinstance(response.get("Answer", []), list):
        raise ClientError("TXT 响应格式无效。")
    texts = [a["data"] for a in response.get("Answer", [])
             if isinstance(a, dict) and a.get("type") == 16 and isinstance(a.get("data"), str)]
    joined = "".join(texts)
    try:
        return _discovered_urls(joined)
    except ClientError:
        pass
    encoded = re.sub(r'\s+|"', '', joined)
    try:
        raw = _base64(encoded)
        plain = bytes(v ^ PROFILE_XOR[i % 16] for i, v in enumerate(raw))
        return _discovered_urls(plain)
    except (ValueError, binascii.Error):
        raise ClientError("TXT 没有有效的客户端发现配置。") from None


def discover_client_api():
    """Fetch public discovery only; do not save/trust its credential destination."""
    failures = []
    for source, url in (("oss", OSS_URL), ("txt", "https://dns.alidns.com/resolve?" +
                        urlencode({"name": TXT_DOMAIN, "type": "TXT"})), ("cos", COS_URL)):
        try:
            with new_session() as session:
                payload, _ = request(session, "GET", url, headers={"Referer": OSS_REFERER})
            if source == "txt":
                response = json.loads(payload)
                return {"source": source, "api_urls": _txt_urls(response), "credentials_sent": False}
            return {"source": source, "api_urls": _discovered_urls(payload), "credentials_sent": False}
        except (ClientError, ValueError, TypeError, KeyError):
            failures.append(source)
    raise ClientError("客户端公开发现服务不可用（" + ", ".join(failures) + "），可手动指定已确认的 API 地址。")

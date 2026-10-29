"""Rootless Mihomo installation and loopback-only process management."""
from __future__ import annotations

import contextlib
import fcntl
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
from html.parser import HTMLParser
import secrets
import shlex
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

from .errors import ClientError
from .network import new_session, _network_error

_RELEASE_ROOT = "https://github.com/MetaCubeX/mihomo/releases/"


class CoreError(ClientError):
    pass


def _atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _request(url, *, headers=None, timeout=30, limit=128 * 1024 * 1024, return_url=False):
    """Use the same explicit proxy/CA mode as account requests, without cookies."""
    import requests
    with new_session() as session:
        for _ in range(6):
            if urllib.parse.urlsplit(url).scheme != "https":
                raise CoreError("Core downloads and redirects must use HTTPS")
            try:
                with session.get(url, headers=headers, timeout=(10, timeout), stream=True,
                                 allow_redirects=False) as res:
                    if res.is_redirect or res.is_permanent_redirect:
                        target = urllib.parse.urljoin(url, res.headers.get("Location", ""))
                        previous = urllib.parse.urlsplit(url).hostname
                        following = urllib.parse.urlsplit(target).hostname
                        github_asset = (previous == "github.com" and following and
                                        (following.endswith(".githubusercontent.com") or following == "github.com"))
                        if not target or (following != previous and not github_asset):
                            raise CoreError("Core download redirected to an unexpected host")
                        url = target
                        continue
                    if res.status_code >= 400:
                        hint = "（可能被拒绝或限流）" if res.status_code in (403, 429) else ""
                        raise CoreError(f"核心下载 HTTP {res.status_code}{hint}；可尝试 core install --direct 或指定官方 --url。")
                    chunks, count = [], 0
                    for chunk in res.iter_content(65536):
                        count += len(chunk)
                        if count > limit:
                            raise CoreError("Download exceeds the allowed size")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    return (data, url) if return_url else data
            except requests.RequestException as exc:
                raise CoreError("核心下载失败：" + str(_network_error(exc, session, url))) from None
    raise CoreError("Core download redirected too many times")


class _ReleaseAssets(HTMLParser):
    """Read each asset and its digest from the same official release list item."""
    def __init__(self):
        super().__init__()
        self.assets, self.href, self.text, self.in_item = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.in_item, self.href, self.text = True, None, []
        if tag == "a" and self.in_item:
            href = dict(attrs).get("href", "")
            if href.startswith("/MetaCubeX/mihomo/releases/download/"):
                self.href = href

    def handle_data(self, data):
        if self.in_item:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "li" and self.in_item:
            if self.href:
                digests = set(re.findall(r"sha256:([0-9a-fA-F]{64})", " ".join(self.text)))
                self.assets.append({"name": self.href.rsplit("/", 1)[-1],
                                    "browser_download_url": "https://github.com" + self.href,
                                    "digest": "sha256:" + next(iter(digests)) if len(digests) == 1 else ""})
            self.in_item = False


def _release_from_page(version):
    if version == "latest":
        _, final_url = _request(_RELEASE_ROOT + "latest", limit=4 * 1024 * 1024, return_url=True)
        if not final_url.startswith(_RELEASE_ROOT + "tag/"):
            raise CoreError("Official release page did not resolve to a release tag")
        version = urllib.parse.unquote(final_url[len(_RELEASE_ROOT + "tag/"):])
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.-]+)?", version):
        raise CoreError("Cannot resolve an official release version; specify --version vX.Y.Z or --url")
    page = _request(_RELEASE_ROOT + "expanded_assets/" + version, limit=8 * 1024 * 1024)
    parser = _ReleaseAssets()
    parser.feed(page.decode("utf-8"))
    return {"tag_name": version, "assets": parser.assets}


def _official_release(version):
    tag = "latest" if version == "latest" else "tags/" + urllib.parse.quote(version, safe="")
    try:
        release = json.loads(_request("https://api.github.com/repos/MetaCubeX/mihomo/releases/" + tag,
                                      limit=4 * 1024 * 1024))
        if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
            raise ValueError("invalid release")
        return release
    except (CoreError, ValueError, UnicodeError):
        # GitHub web pages do not share the anonymous API rate limit.
        return _release_from_page(version)


def _architecture():
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if platform.system() != "Linux" or arch is None:
        raise CoreError("Core installation currently supports Linux amd64 and arm64")
    return arch


def _free_port():
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    except OSError as exc:
        raise CoreError("Cannot allocate a local controller port") from exc


class CoreManager:
    def __init__(self, state_dir, core_path=None):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.core_path = Path(core_path).expanduser().resolve() if core_path else self.state_dir / "bin" / "mihomo"
        self.state_path = self.state_dir / "core-state.json"
        self.runtime_path = self.state_dir / "runtime.yaml"
        self.log_path = self.state_dir / "core.log"
        self.data_dir = self.state_dir / "core-data"
        self._children = {}

    @contextlib.contextmanager
    def _lock(self):
        fd = os.open(self.state_dir / "core.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read_state(self):
        try:
            value = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise CoreError("Invalid core state file") from exc
        if not isinstance(value, dict):
            raise CoreError("Invalid core state file")
        return value

    @staticmethod
    def _identity(pid):
        """Linux PID identity includes boot and process creation time, not just PID."""
        try:
            if type(pid) is not int or pid <= 1:
                return None
            proc = Path("/proc") / str(pid)
            raw = (proc / "stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            if fields[0] == "Z":
                return None
            executable = (proc / "exe").stat()
            return {"start_ticks": fields[19], "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "exe_dev": executable.st_dev, "exe_ino": executable.st_ino}
        except (OSError, IndexError):
            return None

    def _matches(self, state):
        return bool(state and state.get("identity") and self._identity(state.get("pid")) == state["identity"])

    def install(self, url=None, version="latest", sha256=None):
        """Install an official release, or an explicitly selected HTTPS binary/gzip URL."""
        arch = _architecture()
        if sha256 is not None and (len(sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sha256)):
            raise CoreError("SHA256 must contain exactly 64 hexadecimal characters")
        with self._lock():
            if self._matches(self._read_state()):
                raise CoreError("Stop Mihomo before installing a core")
            if url is None:
                release = _official_release(version)
                prefix = f"mihomo-linux-{arch}-"
                assets = [a for a in release.get("assets", []) if a.get("name", "").startswith(prefix)
                          and a.get("name", "").endswith(".gz")]
                if arch == "amd64":
                    compatible = [a for a in assets if a["name"].startswith(prefix + "compatible-")]
                    # Prefer compatible builds; avoid amd64-v2/v3 ISA-specific assets.
                    assets = compatible or [a for a in assets if a["name"].startswith(prefix + "v") and not a["name"].startswith((prefix + "v2-", prefix + "v3-"))]
                if len(assets) != 1:
                    raise CoreError("Could not uniquely select an official release asset; specify --url")
                url = assets[0]["browser_download_url"]
                version = release.get("tag_name", version)
                release_digest = assets[0].get("digest", "")
                if release_digest.startswith("sha256:"):
                    release_sha256 = release_digest[7:]
                    if sha256 and not secrets.compare_digest(sha256.lower(), release_sha256.lower()):
                        raise CoreError("Requested SHA256 differs from official release digest")
                    sha256 = release_sha256
                elif sha256 is None:
                    raise CoreError("官方发布信息缺少 SHA256，无法验证下载；请显式提供 --sha256。")
            parsed_url = urllib.parse.urlsplit(url)
            if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
                raise CoreError("Core URL must not contain credentials, query tokens or a fragment")
            if parsed_url.scheme != "https":
                raise CoreError("Core download URL must use HTTPS")
            payload = _request(url)
            digest = hashlib.sha256(payload).hexdigest()
            if sha256 and not secrets.compare_digest(digest, sha256.lower()):
                raise CoreError("Core download SHA256 mismatch")
            if payload.startswith(b"\x1f\x8b"):
                import io
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
                        payload = compressed.read(128 * 1024 * 1024 + 1)
                except (OSError, EOFError) as exc:
                    raise CoreError("Invalid core gzip archive") from exc
            if len(payload) > 128 * 1024 * 1024:
                raise CoreError("Uncompressed core exceeds the allowed size")
            machine = 62 if arch == "amd64" else 183
            if len(payload) < 64 or payload[:6] != b"\x7fELF\x02\x01" or int.from_bytes(payload[18:20], "little") != machine:
                raise CoreError("Downloaded core is not a compatible 64-bit Linux ELF")
            _atomic(self.core_path, payload, 0o700)
            info = {"path": str(self.core_path), "url": url, "download_sha256": digest,
                    "sha256": hashlib.sha256(payload).hexdigest(), "version": version,
                    "checksum_verified": bool(sha256)}
            _atomic(self.state_dir / "core-install.json", json.dumps(info, indent=2).encode())
            return info

    def install_geodata(self, config_path=None):
        """Install only databases required by the downloaded configuration."""
        try:
            config = yaml.safe_load(Path(config_path or self.state_dir / "subscription.yaml").read_text())
        except (OSError, yaml.YAMLError) as exc:
            raise CoreError("Download a configuration before installing geodata") from exc
        if not isinstance(config, dict) or not isinstance(config.get("rules", []), list):
            raise CoreError("Invalid configuration rules")
        rules = config.get("rules", [])
        if not all(isinstance(rule, str) for rule in rules):
            raise CoreError("Invalid configuration rules")
        needed = []
        if any(re.search(r"(?:^|[,(])GEOSITE,", rule, re.I) for rule in rules):
            needed.append(("GeoSite.dat", "geosite.dat"))
        if any(re.search(r"(?:^|[,(])GEOIP,", rule, re.I) for rule in rules):
            needed.append(("GeoIP.dat", "geoip.dat") if config.get("geodata-mode")
                          else ("Country.mmdb", "country.mmdb"))
        result = []
        with self._lock():
            self.data_dir.mkdir(mode=0o700, exist_ok=True)
            for filename, asset in needed:
                dest = self.data_dir / filename
                if dest.is_file() and dest.stat().st_size > 100:
                    result.append({"name": filename, "downloaded": False})
                    continue
                payload = _request("https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/" + asset)
                valid = (b"\xab\xcd\xefMaxMind.com" in payload[-131072:] if asset.endswith(".mmdb")
                         else payload.startswith(b"\x0a"))
                if len(payload) < 100 or not valid:
                    raise CoreError("Invalid geodata download; existing database unchanged")
                _atomic(dest, payload)
                result.append({"name": filename, "downloaded": True,
                               "sha256": hashlib.sha256(payload).hexdigest()})
        return {"databases": result}

    def _safe_config(self, config, port, controller_port, secret):
        if not isinstance(config, dict):
            raise CoreError("Mihomo config must be a mapping")
        # Clone without mutating the downloaded source configuration.
        try:
            config = json.loads(json.dumps(config))
        except (TypeError, ValueError) as exc:
            raise CoreError("Mihomo configuration contains unsupported values") from exc
        # Remove alternative listeners/controllers and routing side effects.
        for key in ("port", "socks-port", "redir-port", "tproxy-port", "listeners", "tunnels", "iptables",
                    "external-controller", "external-controller-tls", "external-controller-unix", "external-controller-pipe",
                    "external-ui", "external-ui-url", "external-ui-name", "tls", "interface-name", "routing-mark", "authentication",
                    "skip-auth-prefixes", "lan-allowed-ips", "lan-disallowed-ips"):
            config.pop(key, None)
        config.update({"mixed-port": port, "allow-lan": False, "bind-address": "127.0.0.1", "ipv6": False,
                       "external-controller": f"127.0.0.1:{controller_port}", "secret": secret,
                       "tun": {"enable": False}, "dns": {"enable": False}, "ntp": {"enable": False, "write-to-system": False}, "log-level": "info"})
        config["external-controller-cors"] = {"allow-origins": [], "allow-private-network": False}
        config["profile"] = {"store-selected": True, "store-fake-ip": False}
        options = self._options()
        if options.get("mode"):
            config["mode"] = options["mode"]
        if options.get("tun"):
            availability = self.tun_availability()
            if not availability["available"]:
                raise CoreError(availability["reason"] + "; run proxyctl tun off before starting without TUN")
            config["tun"] = self._tun_config(True)
        for kind in ("proxy-providers", "rule-providers"):
            providers = config.get(kind, {})
            if not isinstance(providers, dict):
                raise CoreError("Provider configuration must be a mapping")
            for name, provider in providers.items():
                if not isinstance(provider, dict):
                    raise CoreError("Provider must be a mapping")
                provider_type = provider.get("type", "http")
                if provider_type == "file":
                    raise CoreError("Local file providers from subscriptions are unsupported; use HTTP or inline providers")
                if provider_type not in ("http", "inline"):
                    raise CoreError("Unsupported provider type")
                digest = hashlib.sha256(str(name).encode()).hexdigest()[:24]
                if provider_type == "http":
                    provider["path"] = f"./providers/{kind}-{digest}.yaml"
                else:
                    provider.pop("path", None)
        return config

    def start(self, config_path=None, port=7890, controller_port=None, *, config=None, timeout=15):
        for name, value in (("proxy", port), ("controller", controller_port)):
            if value is not None and (type(value) is not int or not 1024 <= value <= 65535):
                raise CoreError(f"{name} port must be between 1024 and 65535")
        with self._lock():
            old = self._read_state()
            if self._matches(old):
                raise CoreError("Mihomo is already running")
            if not self.core_path.is_file() or not os.access(self.core_path, os.X_OK):
                raise CoreError("Mihomo core not installed; run core install or provide --core")
            if config is None:
                try:
                    config = yaml.safe_load(Path(config_path).read_text())
                except (OSError, yaml.YAMLError, TypeError) as exc:
                    raise CoreError("Cannot read Mihomo configuration") from exc
            controller_port = controller_port or _free_port()
            if controller_port == port:
                raise CoreError("Proxy and controller ports must differ")
            secret = secrets.token_urlsafe(32)
            prepared = self._safe_config(config, port, controller_port, secret)
            self.data_dir.mkdir(mode=0o700, exist_ok=True)
            _atomic(self.runtime_path, yaml.safe_dump(prepared, sort_keys=False, allow_unicode=True).encode())
            command = [str(self.core_path), "-d", str(self.data_dir), "-f", str(self.runtime_path)]
            child_env = {key: value for key, value in os.environ.items() if not key.lower().startswith("_0dcloud_")}
            try:
                result = subprocess.run(command + ["-t"], capture_output=True, timeout=timeout, env=child_env)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CoreError("Mihomo configuration validation could not finish") from exc
            if result.returncode:
                # Do not expose downloaded subscription credentials through errors.
                raise CoreError("Mihomo rejected the runtime configuration; check rules and run core geodata --direct for missing databases")
            fd = os.open(self.log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, start_new_session=True, env=child_env)
            finally:
                os.close(fd)
            self._children[child.pid] = child
            identity = None
            deadline = time.monotonic() + min(timeout, 2)
            while identity is None and child.poll() is None and time.monotonic() < deadline:
                identity = self._identity(child.pid)
                if identity is None:
                    time.sleep(0.02)
            if identity is None:
                child.terminate() if child.poll() is None else None
                child.wait(timeout=3)
                raise CoreError("Mihomo exited before it could start; inspect core.log")
            state = {"pid": child.pid, "identity": identity, "proxy_port": port, "controller_port": controller_port,
                     "secret": secret, "core": str(self.core_path), "started_at": time.time()}
            try:
                _atomic(self.state_path, json.dumps(state, indent=2).encode())
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise CoreError("Mihomo exited during startup; inspect core.log")
                    try:
                        self._api("GET", "/version", state=state)
                        return self.status()
                    except CoreError:
                        time.sleep(0.1)
                raise CoreError("Mihomo controller did not become ready; inspect core.log")
            except BaseException:
                self._stop(state)
                raise

    def _signal_owned(self, state, sig):
        if not self._matches(state):
            return False
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                fd = os.pidfd_open(state["pid"])
                try:
                    if not self._matches(state):
                        return False
                    signal.pidfd_send_signal(fd, sig)
                finally:
                    os.close(fd)
            except ProcessLookupError:
                return False
        else:
            try:
                os.kill(state["pid"], sig)
            except ProcessLookupError:
                return False
        return True

    def _stop(self, state, timeout=5):
        if not self._matches(state):
            return False
        pid = state["pid"]
        if not self._signal_owned(state, signal.SIGTERM):
            return False
        deadline = time.monotonic() + timeout
        while self._matches(state) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._matches(state):
            self._signal_owned(state, signal.SIGKILL)
        child = self._children.pop(pid, None)
        if child:
            child.wait(timeout=3)
        return True

    def stop(self):
        with self._lock():
            state = self._read_state()
            stopped = self._stop(state)
            self.state_path.unlink(missing_ok=True)
            return {"stopped": stopped, "running": False}

    def status(self):
        state = self._read_state()
        running = self._matches(state)
        return {"running": running, "installed": self.core_path.is_file(), "core": str(self.core_path),
                "pid": state["pid"] if running else None,
                "proxy_port": state.get("proxy_port") if running else None,
                "controller_port": state.get("controller_port") if running else None,
                "log": str(self.log_path)}

    def _api(self, method, path, payload=None, *, state=None):
        state = state or self._read_state()
        if not self._matches(state):
            raise CoreError("Mihomo is not running")
        url = f"http://127.0.0.1:{state['controller_port']}{path}"
        req = urllib.request.Request(url, method=method, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"Authorization": "Bearer " + state["secret"], "Content-Type": "application/json"})
        # Local controller requests must never use environment proxy settings.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=3) as res:
                data = res.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise CoreError("Controller response too large")
                return json.loads(data) if data else {}
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise CoreError("Mihomo controller request failed") from exc

    def nodes(self):
        return self._api("GET", "/proxies").get("proxies", {})

    @staticmethod
    def _proxy_route(proxies, name, seen=()):
        """Keep a cached selection when it reaches a proxy; otherwise plan a fallback."""
        item = proxies.get(name)
        if name in seen or not isinstance(item, dict):
            return None
        kind = item.get("type", "").lower()
        if kind in ("direct", "reject", "rejectdrop", "pass", "compatible") or not kind:
            return None
        if "all" not in item:
            return name, []
        current = item.get("now")
        candidates = item["all"] if kind == "selector" else []
        for candidate in dict.fromkeys(([current] if current in item["all"] else []) + candidates):
            route = CoreManager._proxy_route(proxies, candidate, (*seen, name))
            if route is not None:
                node, changes = route
                if kind == "selector" and candidate != current:
                    changes = [*changes, (name, candidate)]
                return node, changes
        return None

    def connect(self, config_path=None, port=7890, controller_port=None, *, group=None):
        """Start if needed, then restore a proxy route or choose the first proxy."""
        if not self.status()["running"]:
            self.start(config_path or self.state_dir / "subscription.yaml", port=port,
                       controller_port=controller_port)
        controls = self.controls()
        mode = controls["mode"]
        proxies = self.nodes()
        if group is None:
            if mode == "global":
                group = "GLOBAL"
            else:
                # The terminal rule identifies the main group in most subscriptions.
                try:
                    config = yaml.safe_load(self.runtime_path.read_text())
                    for rule in reversed(config.get("rules", [])):
                        parts = [part.strip() for part in rule.split(",")]
                        if len(parts) >= 2 and parts[0].upper() in ("MATCH", "FINAL"):
                            if parts[1] in proxies and "all" in proxies[parts[1]]:
                                group = parts[1]
                            break
                except (OSError, yaml.YAMLError):
                    pass
                if group is None:
                    group = next((name for name, item in proxies.items() if name != "GLOBAL"
                                  and item.get("type", "").lower() == "selector"
                                  and self._proxy_route(proxies, name) is not None), None)
        if group not in proxies or "all" not in proxies[group]:
            raise CoreError("没有可用的代理组；请用 nodes 查看配置，并用 --group 指定代理组。")
        route = self._proxy_route(proxies, group)
        if route is None:
            raise CoreError("代理组中没有可选择的代理节点，请更新订阅或用 --group 指定其他代理组。")
        node, changes = route
        if mode == "global" and group != "GLOBAL":
            global_group = proxies.get("GLOBAL", {})
            if group not in global_group.get("all", []):
                raise CoreError("指定的代理组不在 GLOBAL 中，无法用于当前全局模式。")
            if global_group.get("now") != group:
                changes.append(("GLOBAL", group))
        for selected_group, selected_node in changes:
            self.use(selected_group, selected_node)
        if mode == "direct":
            self.mode("rule")
        return {"status": self.status(), "group": group, "node": node}

    def _options(self):
        from .storage import State
        options = State(self.state_dir).read("core-options.json")
        if options.get("mode") not in (None, "rule", "global", "direct") or type(options.get("tun", False)) is not bool:
            raise CoreError("Invalid saved runtime options")
        return options

    @staticmethod
    def _tun_config(enabled):
        # Keep DNS with the system resolver. Do not install global firewall or
        # resolver changes. auto-route is the explicitly requested TUN effect.
        config = {"enable": enabled, "device": "odcloud0", "stack": "mixed", "mtu": 1500,
                "auto-route": enabled, "auto-detect-interface": True,
                "auto-redirect": False, "strict-route": False, "dns-hijack": []}
        if enabled:
            # auto-route otherwise captures port 53 even for on-link resolvers.
            # Some nodes (e.g. AnyTLS) cannot forward UDP. Keep the system's
            # resolver reachable directly, including systemd-resolved upstreams.
            routes = set()
            paths = [Path("/etc/resolv.conf")]
            for path in paths:
                try:
                    text = path.read_text()
                except OSError:
                    continue
                for line in text.splitlines():
                    parts = line.split()
                    if len(parts) < 2 or parts[0] != "nameserver":
                        continue
                    try:
                        address = ipaddress.ip_address(parts[1])
                    except ValueError:
                        continue
                    if address.is_loopback:
                        upstream = Path("/run/systemd/resolve/resolv.conf")
                        if upstream not in paths:
                            paths.append(upstream)
                    elif address.version == 4 and not (address.is_unspecified or address.is_multicast):
                        routes.add(str(address) + "/32")
            config["route-exclude-address"] = sorted(routes)
        return config

    def tun_availability(self):
        if platform.system() != "Linux":
            return {"available": False, "reason": "TUN requires Linux"}
        if not Path("/dev/net/tun").exists():
            return {"available": False, "reason": "TUN device /dev/net/tun is unavailable"}
        try:
            # CapEff reflects the process actually doing the controller update;
            # the core must have been started with the same privileges.
            cap = next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                       if line.startswith("CapEff:"))
            state = self._read_state()
            if self._matches(state):
                cap = next(line.split()[1] for line in Path(f"/proc/{state['pid']}/status").read_text().splitlines()
                           if line.startswith("CapEff:"))
            if not int(cap, 16) & (1 << 12):
                return {"available": False, "reason": "Core requires CAP_NET_ADMIN; start it as root for TUN"}
        except (OSError, StopIteration, ValueError):
            return {"available": False, "reason": "Cannot verify TUN permissions"}
        return {"available": True, "reason": "TUN changes system routing when enabled; DNS keeps the system resolver"}

    def controls(self):
        status = self.status()
        options = self._options()
        config = self._api("GET", "/configs") if status["running"] else {}
        return {"status": status, "mode": config.get("mode", options.get("mode", "rule")),
                "tun": bool(config.get("tun", {}).get("enable", options.get("tun", False))),
                "tun_availability": self.tun_availability()}

    def _set_control(self, key, value):
        from .storage import State
        with self._lock():
            options = self._options()
            running = self.status()["running"]
            previous = self._api("GET", "/configs") if running else {}
            patch = {key: self._tun_config(value) if key == "tun" else value}
            try:
                if running:
                    self._api("PATCH", "/configs", patch)
                    actual = self._api("GET", "/configs")
                    applied = actual.get("tun", {}).get("enable", False) if key == "tun" else actual.get(key)
                    if applied != value:
                        raise CoreError("Core did not apply the requested setting; inspect core.log")
                options[key] = value
                State(self.state_dir).write(options, "core-options.json")
            except (OSError, ClientError):
                if running:
                    try:
                        self._api("PATCH", "/configs", {key: previous.get(key)})
                    except CoreError:
                        raise CoreError("Setting failed and rollback could not be verified; inspect core.log") from None
                raise
        return self.controls()

    def mode(self, value=None):
        if value is None:
            return self.controls()
        if value not in ("rule", "global", "direct"):
            raise CoreError("Proxy mode must be rule, global or direct")
        return self._set_control("mode", value)

    def tun(self, enabled=None):
        if enabled is None:
            return self.controls()
        if type(enabled) is not bool:
            raise CoreError("TUN enable must be boolean")
        if enabled:
            check = self.tun_availability()
            if not check["available"]:
                raise CoreError(check["reason"])
        return self._set_control("tun", enabled)

    def delay(self, node):
        if node not in self.nodes():
            raise CoreError("Unknown proxy node")
        path = "/proxies/" + urllib.parse.quote(node, safe="") + "/delay?" + urllib.parse.urlencode(
            {"url": "https://www.google.com/generate_204", "timeout": 2000})
        return self._api("GET", path)

    def use(self, group, node):
        proxies = self.nodes()
        item = proxies.get(group)
        if not isinstance(item, dict) or item.get("type", "").lower() != "selector":
            raise CoreError("The requested proxy group is not a selectable group")
        if node not in item.get("all", []):
            raise CoreError("The requested node is not in this proxy group")
        self._api("PUT", "/proxies/" + urllib.parse.quote(group, safe=""), {"name": node})
        return {"group": group, "node": node}

    def env(self, shell="sh"):
        state = self._read_state()
        if not self._matches(state):
            raise CoreError("Mihomo is not running")
        port = state["proxy_port"]
        bypass = list(dict.fromkeys(x.strip() for value in (os.environ.get("no_proxy", ""), os.environ.get("NO_PROXY", ""), "localhost,127.0.0.1,::1") for x in value.split(",") if x.strip()))
        values = {"http_proxy": f"http://127.0.0.1:{port}", "https_proxy": f"http://127.0.0.1:{port}",
                  "all_proxy": f"socks5h://127.0.0.1:{port}", "no_proxy": ",".join(bypass)}
        values.update({k.upper(): v for k, v in list(values.items())})
        if shell not in ("sh", "bash", "zsh", "fish"):
            raise CoreError("Supported shells: sh, bash, zsh, fish")
        prefix = "set -gx " if shell == "fish" else "export "
        separator = " " if shell == "fish" else "="
        return "\n".join(prefix + k + separator + shlex.quote(v) + ";" for k, v in values.items())

import base64
import contextlib
import http.server
import json
import io
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import yaml

from odcloud_sh.auth import login, logout, json_result, discover_subscription, update_from_account
from odcloud_sh.cli import main
from odcloud_sh.errors import ClientError
from odcloud_sh.network import new_session, request, validate_url
from odcloud_sh.storage import State
from odcloud_sh.subscription import decode_subscription, set_subscription, update_subscription


CONFIG = {"proxies": [{"name": "test-node", "type": "ss", "server": "127.0.0.1",
                        "port": 8388, "cipher": "aes-128-gcm", "password": "node-test-secret"}],
          "proxy-groups": [{"name": "PROXY", "type": "select", "proxies": ["test-node"]}],
          "rules": ["MATCH,PROXY"]}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, body, headers=None):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
        if self.path == "/auth/login":
            if data == {"email": ["fixture@example.test"], "passwd": ["fixture-password"], "remember_me": ["on"]}:
                self.respond(200, '{"ret":1}', {"Set-Cookie": "sid=fixture-cookie; HttpOnly; Path=/"})
            else:
                self.respond(200, '{"ret":0,"msg":"do not echo fixture-password"}')
        elif self.path == "/clientConfig":
            if self.headers.get("Cookie") != "sid=fixture-cookie":
                self.respond(401, "denied")
            else:
                self.respond(200, json.dumps({"ret": 1, "data": {"yaml": yaml.safe_dump(CONFIG)}}))
        else:
            self.respond(302, "", {"Location": "https://unexpected.example.test/"})

    def do_GET(self):
        self.server.last_cookie = self.headers.get("Cookie")
        if self.path == "/user":
            if self.headers.get("Cookie") != "sid=fixture-cookie":
                self.respond(302, "", {"Location": "/auth/login"})
            else:
                self.respond(200, self.server.dashboard)
        elif self.path == "/subscription?token=fixture-url-secret" or self.path.startswith("/link/good?"):
            self.respond(200, yaml.safe_dump(CONFIG))
        elif self.path == "/redirect":
            self.respond(302, "", {"Location": "https://unexpected.example.test/private-token"})
        elif self.path == "/bad":
            self.respond(500, "fixture-url-secret")
        elif self.path.startswith("/link/bad?"):
            self.respond(504, "fixture-url-secret")
        else:
            self.respond(200, "<html>Login</html>")


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = State(Path(self.temp.name) / "private")
        self.server.dashboard = "<html><button data-clipboard-text='" + self.base + "/link/good?clash=2&amp;token=fixture-url-secret'>Copy</button></html>"
        self.environ = patch.dict(os.environ, {"_0dcloud_mail": "fixture@example.test",
                                               "_0dcloud_pass": "fixture-password", "NO_PROXY": "127.0.0.1"})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def test_login_persists_cookie_but_not_password_and_fetches_config(self):
        self.assertTrue(login(self.state, api_url=self.base)["authenticated"])
        result = update_from_account(self.state)
        self.assertEqual(result["nodes"], 1)
        self.assertEqual(yaml.safe_load(self.state.path("subscription.yaml").read_text()), CONFIG)
        for path in self.state.directory.iterdir():
            self.assertNotIn("fixture-password", path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.directory.stat().st_mode), 0o700)
        logout(self.state)
        self.assertFalse(self.state.path("session.json").exists())

    def test_subscription_uses_fresh_session_and_cached_config_survives_failure(self):
        login(self.state, api_url=self.base)
        set_subscription(self.state, self.base + "/subscription?token=fixture-url-secret")
        update_subscription(self.state)
        self.assertIsNone(self.server.last_cookie)
        previous = self.state.path("subscription.yaml").read_bytes()
        set_subscription(self.state, self.base + "/bad")
        with self.assertRaises(ClientError) as error:
            update_subscription(self.state)
        self.assertNotIn("fixture-url-secret", str(error.exception))
        self.assertEqual(previous, self.state.path("subscription.yaml").read_bytes())

    def test_login_failure_is_not_persisted_or_echoed(self):
        with patch.dict(os.environ, {"_0dcloud_pass": "wrong"}):
            with self.assertRaises(ClientError) as error:
                login(self.state, api_url=self.base)
        self.assertNotIn("fixture-password", str(error.exception))
        self.assertFalse(self.state.path("session.json").exists())

    def test_credentials_are_never_redirected(self):
        with self.assertRaisesRegex(ClientError, "重定向"):
            login(self.state, api_url=self.base, login_path="/redirect-login")
        with new_session() as session:
            with self.assertRaisesRegex(ClientError, "其他站点"):
                request(session, "GET", self.base + "/redirect", redirects=True)

    def test_api_origin_change_refuses_saved_cookie(self):
        login(self.state, api_url=self.base)
        settings = self.state.read()
        settings["api_url"] = "http://127.0.0.1:1"
        self.state.write(settings)
        with self.assertRaisesRegex(ClientError, "其他 API"):
            discover_subscription(self.state)

    def test_decode_yaml_json_base64_and_unicode_nodes(self):
        self.assertEqual(decode_subscription(json.dumps(CONFIG).encode()), CONFIG)
        self.assertEqual(decode_subscription(yaml.safe_dump(CONFIG).encode()), CONFIG)
        credentials = base64.urlsafe_b64encode(b"aes-128-gcm:p@ss:word").decode().rstrip("=")
        uri = f"ss://{credentials}@127.0.0.1:8388#%E9%A6%99%E6%B8%AF"
        for payload in [uri.encode(), base64.b64encode(uri.encode())]:
            config = decode_subscription(payload)
            self.assertEqual(config["proxies"][0]["name"], "香港")
            self.assertEqual(config["proxies"][0]["password"], "p@ss:word")

    def test_reject_unsupported_or_malformed_data(self):
        for payload in [b"<html>login</html>", b"proxies: []", b"vless://unsupported", b"[invalid", b"null"]:
            with self.subTest(payload=payload):
                with self.assertRaises(ClientError):
                    decode_subscription(payload)

    def test_remote_cleartext_and_url_credentials_rejected(self):
        for url in ["http://example.test/sub", "https://user:pass@example.test/", "file:///etc/passwd",
                    "https://example.test/#fragment"]:
            with self.assertRaises(ClientError):
                validate_url(url)

    def test_api_query_is_rejected_before_credentials_are_sent(self):
        with self.assertRaisesRegex(ClientError, "查询参数"):
            login(self.state, api_url=self.base + "/?token=should-not-be-used")

    def test_cookie_state_corruption_is_actionable(self):
        login(self.state, api_url=self.base)
        self.state.write({"api_url": self.base, "cookies": [None]}, "session.json")
        with self.assertRaisesRegex(ClientError, "损坏"):
            discover_subscription(self.state)

    def test_subscription_never_implicitly_loads_netrc(self):
        with new_session() as session, patch("requests.sessions.get_netrc_auth", return_value=("secret", "secret")) as netrc:
            prepared = session.prepare_request(__import__("requests").Request("GET", "https://example.test/sub"))
            self.assertNotIn("Authorization", prepared.headers)
            netrc.assert_not_called()

    def test_percent_encoded_sip002_credentials(self):
        from urllib.parse import quote
        credentials = base64.urlsafe_b64encode(b"aes-128-gcm:password").decode()
        uri = "ss://" + quote(credentials, safe="") + "@127.0.0.1:8388#node"
        self.assertEqual(decode_subscription(uri.encode())["proxies"][0]["password"], "password")

    def test_two_factor_response_has_actionable_message(self):
        with self.assertRaisesRegex(ClientError, "_0dcloud_otp"):
            json_result(b'{"ret":2,"msg":"remote content must not echo"}')

    def test_success_false_is_not_confused_with_numeric_success(self):
        for payload in [b'{"success":false}', b'{"ret":0}', b'{"ret":"1"}', b'{}']:
            with self.assertRaises(ClientError):
                json_result(payload)
        self.assertEqual(json_result(b'{"success":true}'), {"success": True})

    def test_dashboard_discovery_and_cli_update_from_account(self):
        login(self.state, api_url=self.base)
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(main(["--state-dir", str(self.state.directory), "update", "--from-account"]), 0)
        self.assertEqual(yaml.safe_load(self.state.path("subscription.yaml").read_text()), CONFIG)
        self.assertIn("clash=2", self.state.read()["subscription_url"])
        self.assertNotIn("fixture-url-secret", output.getvalue() + errors.getvalue())
        self.assertIsNone(self.server.last_cookie)

    def test_failed_discovered_subscription_preserves_url_and_config(self):
        login(self.state, api_url=self.base)
        set_subscription(self.state, self.base + "/subscription?token=fixture-url-secret")
        update_subscription(self.state)
        before = self.state.read()
        config = self.state.path("subscription.yaml").read_bytes()
        self.server.dashboard = "<a href='" + self.base + "/link/bad?clash=2'>Copy</a>"
        with self.assertRaisesRegex(ClientError, "504"):
            update_from_account(self.state)
        self.assertEqual(self.state.read(), before)
        self.assertEqual(self.state.path("subscription.yaml").read_bytes(), config)

    def test_dashboard_prefers_clash2_and_falls_back_to_clash1(self):
        login(self.state, api_url=self.base)
        url1 = self.base + "/link/good?clash=1"
        url2 = self.base + "/link/good?clash=2"
        self.server.dashboard = f"<a href='{url1}'>Old</a><script>var x='{url2}';</script>"
        self.assertEqual(discover_subscription(self.state), url2)
        self.server.dashboard = f"<a href='{url1}'>Old</a>"
        self.assertEqual(discover_subscription(self.state), url1)

    def test_dashboard_login_form_missing_links_and_ambiguous_links_fail(self):
        login(self.state, api_url=self.base)
        for html in ["<form id='login_form'></form>", "<html>Dashboard without subscription</html>",
                     "<a href='https://example.test/link/one?clash=2'>One</a><a href='https://example.test/link/two?clash=2'>Two</a>"]:
            self.server.dashboard = html
            with self.assertRaises(ClientError):
                discover_subscription(self.state)


if __name__ == "__main__":
    unittest.main()

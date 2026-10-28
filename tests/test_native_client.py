import base64
import contextlib
import hashlib
import hmac
import http.server
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import yaml

from odcloud_sh.client_api import (client_signature, decode_client_response,
                                   discover_client_api, fetch_client_config, login_client,
                                   _discovered_urls, _txt_urls)
from odcloud_sh.cli import main
from odcloud_sh.errors import ClientError
from odcloud_sh.storage import State

EMAIL, PASSWORD = "fixture@example.test", "private-fixture-password"
CONFIG = {"proxies": [{"name": "native", "type": "ss", "server": "127.0.0.1",
                       "port": 8388, "cipher": "aes-128-gcm", "password": "fixture-node"}]}


def envelope():
    # Independent server fixture follows recovered wire format, not client helpers.
    iv = bytes.fromhex("000102030405060708090a0b")
    key = hashlib.sha256(b"XE5P0YBMvqfzZLAS7C7wNZYS984tNzqzIz4yAnH_tjU").digest()
    body = AESGCM(key).encrypt(iv, yaml.safe_dump(CONFIG).encode(), None)
    return {"ret": 1, "enc": "aes-256-gcm", "data": base64.b64encode(iv + body).decode()}


class NativeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, value, *, cookie=False):
        body = json.dumps(value).encode()
        self.send_response(200)
        if cookie:
            self.send_header("Set-Cookie", "native_sid=fixture; Path=/; HttpOnly")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        if urlsplit(self.path).path == "/authorization":
            self.server.login_query = query
            self.respond({"ret": 1 if query == {"email": [EMAIL], "passwd": [PASSWORD]} else 0}, cookie=True)

    def do_POST(self):
        fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        self.server.requests.append((fields, dict(self.headers)))
        msg = "\n".join(("POST", "/clientConfig", self.headers["X-Client-Ts"],
                          self.headers["X-Client-Nonce"], self.headers["X-Client-Id"], EMAIL))
        signature = hmac.new(b"CQl9EwqeY4kK0wYVUjJsfsDgbFZOCKhZCjtQ0tFAupE",
                             msg.encode(), hashlib.sha256).hexdigest()
        ok = (fields == {"email": [EMAIL], "passwd": [PASSWORD], "platform": ["linux"]}
              and hmac.compare_digest(signature, self.headers["X-Client-Sig"])
              and self.headers.get("Cookie") == "native_sid=fixture")
        self.respond(self.server.response if ok else {"ret": 0, "msg": PASSWORD})


class NativeClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NativeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = State(temporary.name)
        env = patch.dict(os.environ, {"_0dcloud_mail": EMAIL, "_0dcloud_pass": PASSWORD,
                                      "NO_PROXY": "127.0.0.1"})
        env.start(); self.addCleanup(env.stop)
        self.server.requests = []
        self.server.response = envelope()

    def test_signature_vector_matches_independent_node_crypto(self):
        self.assertEqual(client_signature("abcdef0123456789abcdef0123456789", EMAIL,
                                         "1700000000", "0123456789abcdef0123456789abcdef"),
                         "0d3b3ee7352be00013d7f266823f38eca8e431b3945408b619ed53a2b2ca5ca3")

    def test_native_login_and_signed_encrypted_refresh_without_subscription_url(self):
        login_client(self.state, api_url=self.base)
        result = fetch_client_config(self.state)
        self.assertEqual(result["nodes"], 1)
        config = yaml.safe_load(self.state.path("subscription.yaml").read_text())
        self.assertEqual(config["proxies"], CONFIG["proxies"])
        fetch_client_config(self.state)
        first, second = [x[1] for x in self.server.requests]
        self.assertEqual(first["X-Client-Id"], second["X-Client-Id"])
        self.assertRegex(first["X-Client-Id"], r"^[0-9a-f]{32}$")
        self.assertRegex(first["X-Client-Nonce"], r"^[0-9a-f]{32}$")
        self.assertNotEqual(first["X-Client-Nonce"], second["X-Client-Nonce"])
        self.assertEqual(first["User-Agent"], "0dcloud/2026")
        self.assertEqual(first["X-App-Version"], "2.0.30")
        self.assertRegex(first["X-Client-Ts"], r"^\d{10}$")
        for file in self.state.directory.iterdir():
            self.assertNotIn(PASSWORD, file.read_text())
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("subscription_url", self.state.read())

    def test_bad_gcm_tag_preserves_previous_configuration(self):
        login_client(self.state, api_url=self.base)
        fetch_client_config(self.state)
        previous = self.state.path("subscription.yaml").read_bytes()
        raw = bytearray(base64.b64decode(self.server.response["data"]))
        raw[-1] ^= 1
        self.server.response["data"] = base64.b64encode(raw).decode()
        with self.assertRaisesRegex(ClientError, "认证失败"):
            fetch_client_config(self.state)
        self.assertEqual(self.state.path("subscription.yaml").read_bytes(), previous)

    def test_plain_and_base64_responses_and_failure_redaction(self):
        plain = yaml.safe_dump(CONFIG)
        for body in (plain, base64.b64encode(plain.encode()).decode()):
            self.assertEqual(decode_client_response(json.dumps({"ret": 1, "data": body}).encode())["proxies"], CONFIG["proxies"])
        for response in ({"ret": 0, "msg": PASSWORD}, {"ret": True},
                         {"ret": 1, "enc": "aes-256-gcm", "data": "invalid"},
                         {"ret": 1, "enc": "future-cipher", "data": PASSWORD}):
            with self.assertRaises(ClientError) as caught:
                decode_client_response(json.dumps(response).encode())
            self.assertNotIn(PASSWORD, str(caught.exception))

    def test_http_requires_explicit_opt_in_on_each_command(self):
        with patch("odcloud_sh.client_api.request") as call:
            with self.assertRaises(ClientError):
                login_client(self.state, api_url="http://192.0.2.1:7985")
            call.assert_not_called()
        self.state.write({"api_url": "http://192.0.2.1:7985"}, "client-session.json")
        with self.assertRaises(ClientError):
            fetch_client_config(self.state)

    def test_account_mismatch_and_missing_password_do_not_send_request(self):
        login_client(self.state, api_url=self.base)
        for change in ({"_0dcloud_mail": "another@example.test"}, {"_0dcloud_pass": ""}):
            with patch.dict(os.environ, change), patch("odcloud_sh.client_api.request") as call:
                with self.assertRaises(ClientError):
                    fetch_client_config(self.state)
                call.assert_not_called()

    def test_cli_native_flow_and_logout_preserves_device(self):
        root = ["--state-dir", str(self.state.directory)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(root + ["login", "--client-api", "--api-url", self.base]), 0)
            self.assertEqual(main(root + ["update", "--from-client-api"]), 0)
            self.assertEqual(main(root + ["logout"]), 0)
        self.assertFalse(self.state.path("client-session.json").exists())
        self.assertTrue(self.state.path("client-device.json").exists())

    def test_discovery_does_not_send_account_cookie_or_save_target(self):
        with patch("odcloud_sh.client_api.request", return_value=(b'{"api_url":"http://192.0.2.1:7985"}', {})) as call:
            result = discover_client_api()
        session, method, url = call.call_args.args
        self.assertFalse(session.cookies)
        self.assertEqual(method, "GET")
        self.assertNotIn("data", call.call_args.kwargs)
        self.assertFalse(result["credentials_sent"])
        self.assertEqual(result["api_urls"], ["http://192.0.2.1:7985"])
        self.assertFalse(self.state.path("client-session.json").exists())

    def test_discovery_api_order_nested_lists_and_encrypted_split_txt(self):
        config = {"api_url": " https://first.example.test/// ",
                  "api_url_fallback": [["https://second.example.test", "https://first.example.test"]],
                  "api_urls": ["https://third.example.test", "https://你的地址"]}
        expected = ["https://first.example.test", "https://second.example.test", "https://third.example.test"]
        self.assertEqual(_discovered_urls(json.dumps(config)), expected)
        key = bytes((163,92,225,114,159,75,216,38,62,192,87,138,29,244,107,147))
        plain = json.dumps(config).encode()
        encoded = base64.b64encode(bytes(v ^ key[i % 16] for i, v in enumerate(plain))).decode()
        response = {"Answer": [{"type": 1, "data": "192.0.2.1"},
                               {"type": 16, "data": '"' + encoded[:40] + '"'},
                               {"type": 16, "data": '"' + encoded[40:] + '"'}]}
        self.assertEqual(_txt_urls(response), expected)


if __name__ == "__main__":
    unittest.main()

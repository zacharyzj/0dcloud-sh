import contextlib
import io
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

import requests

from odcloud_sh.cli import main, parser
from odcloud_sh.errors import ClientError
from odcloud_sh.network import network_mode, new_session, request


class NetworkErrorTests(unittest.TestCase):
    def error(self, exception, *, proxies=None):
        with new_session() as session:
            session.trust_env = False
            session.proxies = proxies or {}
            with patch.object(session, "request", side_effect=exception):
                with self.assertRaises(ClientError) as caught:
                    request(session, "POST", "https://example.test/login?token=secret-token",
                            data={"passwd": "secret-password"})
        message = str(caught.exception)
        for secret in ("secret-token", "secret-password", "proxy-user", "proxy-password"):
            self.assertNotIn(secret, message)
        return message

    def test_proxy_error_classified_without_leaking_url_or_credentials(self):
        message = self.error(requests.exceptions.ProxyError(
            "https://proxy-user:proxy-password@proxy.test/?token=secret-token"),
            proxies={"https": "http://proxy-user:proxy-password@proxy.test:8080"})
        self.assertIn("ProxyError", message)
        self.assertIn("使用环境或会话代理", message)
        self.assertIn("--direct", message)

    def test_ssl_timeout_dns_and_refused_are_distinguishable(self):
        cases = [(requests.exceptions.SSLError("secret-token"), "SSLError"),
                 (requests.exceptions.ConnectTimeout("secret-token"), "ConnectTimeout"),
                 (requests.exceptions.ReadTimeout("secret-token"), "ReadTimeout"),
                 (requests.exceptions.ConnectionError(socket.gaierror(-2, "secret-token")), "DNS"),
                 (requests.exceptions.ConnectionError(ConnectionRefusedError(111, "secret-token")), "ConnectionRefused"),
                 (requests.exceptions.InvalidSchema("secret-token"), "PySocks")]
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, self.error(error))

    def test_direct_mode_is_scoped_and_preserves_ca_environment(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.test:8080",
                                     "REQUESTS_CA_BUNDLE": "/tmp/custom-ca.pem"}):
            with network_mode(direct=True):
                with new_session() as session:
                    self.assertFalse(session.trust_env)
                    self.assertEqual(session.verify, "/tmp/custom-ca.pem")
            with new_session() as session:
                self.assertTrue(session.trust_env)

    def test_cli_direct_option_supported_for_account_and_subscription(self):
        for command in ("login", "update", "doctor"):
            self.assertTrue(parser().parse_args([command, "--direct"]).direct)

    def test_doctor_sends_no_account_credentials_or_saved_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            from odcloud_sh.storage import State
            state = State(directory)
            state.write({"api_url": "https://example.test", "cookies": [{"name": "sid", "value": "secret"}]}, "session.json")
            output = io.StringIO()
            with patch("odcloud_sh.cli.request", return_value=(b"page", {})) as call, contextlib.redirect_stdout(output):
                self.assertEqual(main(["--state-dir", directory, "doctor", "--api-url", "https://example.test", "--direct"]), 0)
            session, method, url = call.call_args.args
            self.assertFalse(session.cookies)
            self.assertEqual(method, "GET")
            self.assertNotIn("data", call.call_args.kwargs)
            self.assertIn("未发送账号凭据", output.getvalue())


if __name__ == "__main__":
    unittest.main()

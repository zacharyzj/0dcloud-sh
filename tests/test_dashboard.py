import copy
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

from odcloud_sh.cli import parser
from odcloud_sh.dashboard import DashboardServer, display_host, show_url
from odcloud_sh.errors import ClientError
from odcloud_sh.storage import State


class FakeCore:
    def __init__(self, directory):
        self.state_dir = Path(directory)
        self.value = {
            'status': {'running': True, 'installed': True, 'pid': 123, 'proxy_port': 7890,
                       'controller_port': 12345, 'core': '/private/core', 'log': '/private/log'},
            'mode': 'rule', 'tun': False,
            'tun_availability': {'available': False, 'reason': 'no permission'},
        }
        self.selected = 'one'

    def controls(self):
        return copy.deepcopy(self.value)

    def nodes(self):
        return {'Proxy': {'type': 'Selector', 'all': ['one', 'two'], 'now': self.selected},
                'one': {'type': 'AnyTLS', 'password': 'must-not-expose', 'server': 'private-host'},
                'two': {'type': 'AnyTLS', 'history': [{'delay': 50}], 'udp': True}}

    def mode(self, mode):
        self.value['mode'] = mode
        return self.controls()

    def tun(self, enabled):
        if enabled:
            raise ClientError('no permission')
        self.value['tun'] = False
        return self.controls()

    def use(self, group, node):
        if group != 'Proxy' or node not in ('one', 'two'):
            raise ClientError('unknown selection')
        self.selected = node
        return {'group': group, 'node': node}

    def delay(self, node):
        return {'delay': 42}

    def start(self, path, port=7890):
        assert path == self.state_dir / 'subscription.yaml'
        self.value['status']['running'] = True
        self.value['status']['proxy_port'] = port
        return {'running': True}

    def stop(self):
        self.value['status']['running'] = False
        return {'running': False}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = FakeCore(self.temp.name)
        # Choose a free unprivileged port without hardcoding one across runners.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.password = 'fixture-password-测试'
        with mock.patch.dict(os.environ, {'_0dcloud_pass': self.password}):
            self.server = DashboardServer(self.core, port=port)
        self.cookie = None
        self.assertEqual(self.server.server_address[0], '0.0.0.0')
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=2)
        self.addCleanup(cleanup)

    def request(self, path, data=None, *, auth=True, headers=None, raw=None):
        if auth and self.cookie is None:
            status, response_headers, _ = self.request('/api/login', {'password': self.password}, auth=False)
            self.assertEqual(status, 200)
            self.cookie = response_headers['Set-Cookie'].split(';', 1)[0]
        values = {'Cookie': self.cookie} if auth else {}
        values.update(headers or {})
        body = None
        if data is not None or raw is not None:
            body = raw if raw is not None else json.dumps(data)
            values.setdefault('Content-Type', 'application/json')
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            conn.request('POST' if body is not None else 'GET', path, body=body, headers=values)
            response = conn.getresponse()
            text = response.read().decode()
            value = json.loads(text) if response.getheader('Content-Type', '').startswith('application/json') else text
            return response.status, dict(response.getheaders()), value
        finally:
            conn.close()

    def test_public_shell_and_authenticated_private_state(self):
        status, headers, page = self.request('/', auth=False)
        self.assertEqual(status, 200)
        self.assertIn('0dcloud', page)
        self.assertNotIn(self.password, page)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(self.request('/api/state', auth=False)[0], 401)
        self.assertEqual(self.request('/api/state', headers={'Cookie': 'odcloud_session=wrong'})[0], 401)
        status, headers, data = self.request('/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(data['proxies']['Proxy']['now'], 'one')
        self.assertEqual(data['proxies']['two']['history'], [{'delay': 50}])
        for secret in ('password', 'must-not-expose', 'private-host', '/private/', 'controller_port'):
            self.assertNotIn(secret, json.dumps(data))
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_actions_require_auth_and_reject_cross_site(self):
        self.assertEqual(self.request('/api/mode', {'mode': 'global'}, auth=False)[0], 401)
        self.assertEqual(self.request('/api/mode', {'mode': 'global'}, headers={'Origin': 'https://foreign.test'})[0], 403)
        self.assertEqual(self.request('/api/state', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.core.value['mode'], 'rule')
        origin = f'http://127.0.0.1:{self.server.server_port}'
        self.assertEqual(self.request('/api/mode', {'mode': 'global'}, headers={'Origin': origin})[0], 200)
        self.assertEqual(self.request('/api/mode', {'mode': 'rule'}, headers={'Origin': origin.replace('http:', 'https:')})[0], 200)

    def test_select_mode_tun_and_lifecycle(self):
        self.assertEqual(self.request('/api/use', {'group': 'Proxy', 'node': 'two'})[0], 200)
        self.assertEqual(self.core.selected, 'two')
        self.assertEqual(self.request('/api/mode', {'mode': 'direct'})[0], 200)
        self.assertEqual(self.core.value['mode'], 'direct')
        self.assertEqual(self.request('/api/tun', {'enabled': True})[0], 409)
        self.assertFalse(self.core.value['tun'])
        self.assertEqual(self.request('/api/tun', {'enabled': False})[0], 200)
        self.assertEqual(self.request('/api/delay', {'node': 'one'})[2], {'delay': 42})
        self.assertEqual(self.request('/api/stop', {})[0], 200)
        self.assertEqual(self.request('/api/state')[2]['proxies'], {})
        self.assertEqual(self.request('/api/start', {})[0], 200)
        self.assertTrue(self.request('/api/state')[2]['status']['running'])

    def test_only_fixed_validated_actions(self):
        for path, data in (('/api/mode', {'mode': '../bad'}), ('/api/tun', {'enabled': 'on'}),
                           ('/api/use', {'group': [], 'node': 'one'}), ('/api/delay', {'node': {}})):
            self.assertEqual(self.request(path, data)[0], 409)
        for raw in ('[]', '{', 'x' * 8193):
            self.assertEqual(self.request('/api/start', raw=raw)[0], 400)
        self.assertEqual(self.request('/api/start', {}, headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('/api/configs', {})[0], 404)
        self.assertEqual(self.request('/../../settings.json')[0], 404)
        self.assertEqual(self.request('/api/state?token=old-token', auth=False)[0], 404)

    def test_connect_disconnect_idempotent_and_preserve_settings(self):
        self.core.value['status']['proxy_port'] = 17890
        self.core.value['mode'] = 'global'
        self.core.selected = 'two'
        for _ in range(2):
            self.assertEqual(self.request('/api/disconnect', {})[2], {'connected': False})
        self.assertEqual(self.request('/api/connect', {}, auth=False)[0], 401)
        self.assertFalse(self.core.value['status']['running'])
        for _ in range(2):
            self.assertEqual(self.request('/api/connect', {})[2], {'connected': True})
        self.assertEqual(self.core.value['status']['proxy_port'], 17890)
        self.assertEqual(self.core.value['mode'], 'global')
        self.assertEqual(self.core.selected, 'two')

    def test_connect_failure_does_not_claim_connected(self):
        self.request('/api/disconnect', {})
        with mock.patch.object(self.core, 'start', side_effect=ClientError('configuration missing')):
            status, _, data = self.request('/api/connect', {})
        self.assertEqual(status, 409)
        self.assertIn('configuration missing', data['error'])
        self.assertFalse(self.request('/api/state')[2]['status']['running'])

    def test_cli_defaults_and_private_access_file(self):
        args = parser().parse_args(['dashboard'])
        self.assertEqual((args.host, args.port), ('0.0.0.0', 8787))
        state = State(self.temp.name)
        with self.assertRaises(ClientError):
            show_url(state)
        state.write({'port': self.server.server_port, 'token': 'old-token'}, 'dashboard-access.json')
        self.assertEqual(show_url(state), f'http://127.0.0.1:{self.server.server_port}/')
        self.assertEqual(state.path('dashboard-access.json').stat().st_mode & 0o777, 0o600)
        for host, port in (('example.test', 8787), ('0.0.0.0', 80), ('0.0.0.0', True)):
            with self.assertRaises(ClientError):
                DashboardServer(self.core, host, port)

    def test_password_login_logout_and_expiry(self):
        self.assertEqual(self.request('/api/login', {'password': 'wrong'}, auth=False)[0], 401)
        status, headers, data = self.request('/api/login', {'password': self.password}, auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(data['authenticated'])
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertNotIn(self.password, json.dumps(data))
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.assertEqual(self.request('/api/state', auth=False, headers={'Cookie': cookie})[0], 200)
        self.assertEqual(self.request('/api/logout', {}, auth=False, headers={'Cookie': cookie})[0], 200)
        self.assertEqual(self.request('/api/state', auth=False, headers={'Cookie': cookie})[0], 401)
        status, headers, _ = self.request('/api/login', {'password': self.password}, auth=False)
        for key in self.server.sessions:
            self.server.sessions[key] = 0
        self.assertEqual(self.request('/api/state', auth=False, headers={'Cookie': headers['Set-Cookie'].split(';', 1)[0]})[0], 401)

    def test_login_cross_origin_rate_limit_and_missing_password(self):
        self.assertEqual(self.request('/api/login', {'password': self.password}, auth=False,
                                      headers={'Origin': 'https://foreign.test'})[0], 403)
        for _ in range(10):
            self.assertEqual(self.request('/api/login', {'password': 'bad'}, auth=False)[0], 401)
        self.assertEqual(self.request('/api/login', {'password': self.password}, auth=False)[0], 429)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ClientError, '_0dcloud_pass'):
                DashboardServer(self.core, port=18787)

    def test_access_address_auto_detect_and_cloud_nat_override(self):
        state = State(self.temp.name)
        with mock.patch('odcloud_sh.dashboard.socket.socket') as factory:
            factory.return_value.__enter__.return_value.getsockname.return_value = ('10.2.0.7', 12345)
            self.assertEqual(display_host('0.0.0.0'), '10.2.0.7')
        state.write({'host': '0.0.0.0', 'display_host': '152.136.54.246',
                     'public_host': '152.136.54.246', 'port': 8787, 'token': 'fixture'}, 'dashboard-access.json')
        self.assertEqual(show_url(state), 'http://152.136.54.246:8787/')
        self.assertEqual(display_host('127.0.0.1', '152.136.54.246'), '127.0.0.1')
        for address in ('bad/host', '0.0.0.0', '127.0.0.1', '224.0.0.1'):
            with self.assertRaises(ClientError):
                display_host('0.0.0.0', address)


if __name__ == '__main__':
    unittest.main()

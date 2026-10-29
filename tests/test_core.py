import json
import hashlib
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import yaml

from odcloud_sh import core
from odcloud_sh.core import CoreError, CoreManager

FAKE_CORE = r'''#!/usr/bin/env python3
import sys, json, signal, os
import http.server
import yaml
from pathlib import Path
cfg = yaml.safe_load(Path(sys.argv[sys.argv.index('-f') + 1]).read_text())
assert not any(k.lower().startswith('_0dcloud_') for k in os.environ)
if '-t' in sys.argv:
    sys.exit(1 if cfg.get('reject_test') else 0)
if cfg.get('exit_test'):
    sys.exit(1)
proxies = {'Select': {'type': 'Selector', 'all': ['one', 'two'], 'now': 'one'},
           'one': {'type': 'Shadowsocks'}, 'two': {'type': 'Shadowsocks'}}
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer ' + cfg['secret']:
            self.send_error(401); return
        body = json.dumps({'version':'fake'} if self.path == '/version' else
                          cfg if self.path == '/configs' else {'proxies':proxies}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def do_PATCH(self):
        if self.headers.get('Authorization') != 'Bearer ' + cfg['secret']:
            self.send_error(401); return
        cfg.update(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.send_response(204); self.end_headers()
    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        proxies['Select']['now'] = body['name']
        self.send_response(204); self.end_headers()
port = int(cfg['external-controller'].rsplit(':', 1)[1])
http.server.HTTPServer(('127.0.0.1',port), Handler).serve_forever()
'''


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        fake = root / 'fake-core'
        fake.write_text(FAKE_CORE.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        fake.chmod(0o700)
        self.manager = CoreManager(root / 'state', fake)
        self.addCleanup(self.manager.stop)

    def start(self, **extra):
        return self.manager.start(config={'proxies': [], 'proxy-groups': [], 'rules': ['MATCH,DIRECT'], **extra}, timeout=4)

    def test_lifecycle_controller_nodes_and_env(self):
        status = self.start()
        self.assertTrue(status['running'])
        self.assertNotIn('secret', status)
        self.assertNotIn('secret', json.dumps(self.manager.nodes()))
        self.assertEqual(self.manager.nodes()['Select']['now'], 'one')
        self.manager.use('Select', 'two')
        self.assertEqual(self.manager.nodes()['Select']['now'], 'two')
        with self.assertRaises(CoreError):
            self.manager.use('Select', 'missing')
        with self.assertRaises(CoreError):
            self.manager.use('one', 'two')
        self.assertIn('socks5h://127.0.0.1:7890', self.manager.env())
        self.assertIn('set -gx', self.manager.env('fish'))
        with self.assertRaises(CoreError):
            self.start()
        self.assertTrue(self.manager.stop()['stopped'])
        self.assertFalse(self.manager.status()['running'])
        with self.assertRaises(CoreError):
            self.manager.nodes()

    def test_config_removes_network_side_effects_and_uses_strong_secret(self):
        original = {'proxies': [{'name': 'one'}], 'tun': {'enable': True}, 'dns': {'enable': True, 'listen':'0.0.0.0:53'},
                    'port': 80, 'redir-port': 81, 'tproxy-port':82, 'external-controller':'0.0.0.0:90',
                    'external-controller-unix':'/tmp/exposed', 'listeners':[{'port':123}], 'tunnels':['anything'],
                    'external-ui-url':'https://example.invalid', 'routing-mark':7}
        runtime = self.manager._safe_config(original, 7890, 9090, 'random')
        self.assertEqual(runtime['proxies'], original['proxies'])
        self.assertTrue(original['tun']['enable'])
        self.assertFalse(runtime['tun']['enable'])
        self.assertEqual(runtime['dns'], {'enable':False})
        self.assertFalse(runtime['allow-lan'])
        self.assertEqual(runtime['external-controller'], '127.0.0.1:9090')
        self.assertTrue(runtime['profile']['store-selected'])
        for key in ('port', 'redir-port', 'tproxy-port', 'external-controller-unix', 'listeners', 'tunnels', 'external-ui-url', 'routing-mark'):
            self.assertNotIn(key, runtime)
        self.start()
        config = yaml.safe_load(self.manager.runtime_path.read_text())
        self.assertGreaterEqual(len(config['secret']), 40)
        for path in (self.manager.runtime_path, self.manager.state_path, self.manager.log_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.manager.state_dir.stat().st_mode), 0o700)

    def test_connect_starts_then_reuses_core_and_keeps_current_node(self):
        config_path = self.manager.state_dir / 'subscription.yaml'
        config_path.write_text(yaml.safe_dump({'rules': ['MATCH,Select']}))
        first = self.manager.connect(config_path)
        self.assertTrue(first['status']['running'])
        self.assertEqual((first['group'], first['node']), ('Select', 'one'))
        self.manager.use('Select', 'two')
        second = self.manager.connect(config_path)
        self.assertEqual(second['status']['pid'], first['status']['pid'])
        self.assertEqual(second['node'], 'two')

    def test_configuration_is_validated_before_launch(self):
        with self.assertRaisesRegex(CoreError, 'rejected'):
            self.start(reject_test=True)
        self.assertFalse(self.manager.state_path.exists())
        self.assertFalse(self.manager.log_path.exists())
        with self.assertRaises(CoreError):
            self.manager.start(config=[])

    def test_early_exit_is_cleaned_up(self):
        with self.assertRaises(CoreError):
            self.start(exit_test=True)
        self.assertFalse(self.manager.status()['running'])

    def test_pid_reuse_or_forged_identity_never_kills(self):
        fake = {'pid': os.getpid(), 'identity': {'start_ticks': 'wrong'}}
        self.manager.state_path.write_text(json.dumps(fake))
        with mock.patch.object(core.os, 'kill') as kill:
            self.assertFalse(self.manager.stop()['stopped'])
            kill.assert_not_called()

    def test_status_does_not_trust_pid_alone(self):
        self.manager.state_path.write_text(json.dumps({'pid': os.getpid()}))
        self.assertFalse(self.manager.status()['running'])

    def test_api_bypasses_environment_proxies(self):
        self.start()
        with mock.patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'HTTP_PROXY':'http://127.0.0.1:1', 'no_proxy':'', 'NO_PROXY':''}):
            self.assertIn('Select', self.manager.nodes())

    def test_install_checks_hash_architecture_and_running_process(self):
        payload = bytearray(64)
        payload[:6] = b'\x7fELF\x02\x01'
        payload[18:20] = (62).to_bytes(2, 'little')
        self.manager.core_path = self.manager.state_dir / 'bin' / 'mihomo'
        with mock.patch.object(core, '_architecture', return_value='amd64'), mock.patch.object(core, '_request', return_value=bytes(payload)):
            with self.assertRaisesRegex(CoreError, 'SHA256'):
                self.manager.install(url='https://example.test/mihomo', sha256='0'*64)
            result = self.manager.install(url='https://example.test/mihomo')
            self.assertEqual(Path(result['path']).read_bytes(), bytes(payload))
            self.assertEqual(stat.S_IMODE(self.manager.core_path.stat().st_mode), 0o700)
            with self.assertRaisesRegex(CoreError, 'HTTPS'):
                self.manager.install(url='http://example.test/mihomo')
        with mock.patch.object(core, '_architecture', return_value='amd64'), mock.patch.object(core, '_request', return_value=b'not an elf'):
            with self.assertRaisesRegex(CoreError, 'ELF'):
                self.manager.install(url='https://example.test/mihomo')

    def test_core_never_inherits_account_credentials(self):
        with mock.patch.dict(os.environ, {"_0dcloud_mail":"fake@example.test", "_0dcloud_pass":"fixture-only", "_0dcloud_sub":"fixture-only"}):
            self.assertTrue(self.start()["running"])

    def test_env_preserves_bypass_and_emits_both_cases(self):
        self.start()
        with mock.patch.dict(os.environ, {"NO_PROXY":".corp.test,localhost", "no_proxy":".internal.test,localhost"}):
            result = self.manager.env()
            self.assertIn("export HTTPS_PROXY=", result)
            self.assertIn("export https_proxy=", result)
            self.assertIn(".corp.test", result)
            self.assertIn(".internal.test", result)

    def test_provider_paths_are_scoped_and_file_providers_rejected(self):
        config = {"proxy-providers": {"../../escape": {"type":"http", "url":"https://example.test/sub", "path":"/etc/escape"}},
                  "rule-providers": {"rules": {"type":"http", "path":"../../escape"}}}
        result = self.manager._safe_config(config, 7890, 9090, "fixture")
        for kind in ("proxy-providers", "rule-providers"):
            path = next(iter(result[kind].values()))["path"]
            self.assertTrue(path.startswith("./providers/"))
            self.assertNotIn("..", path)
        self.assertEqual(config["proxy-providers"]["../../escape"]["path"], "/etc/escape")
        with self.assertRaisesRegex(CoreError, "Local file"):
            self.manager._safe_config({"proxy-providers":{"x":{"type":"file","path":"/etc/passwd"}}},7890,9090,"fixture")

    def test_official_asset_digest_is_verified_and_version_recorded(self):
        payload = bytearray(64)
        payload[:6] = b'\x7fELF\x02\x01'
        payload[18:20] = (62).to_bytes(2, 'little')
        digest = hashlib.sha256(payload).hexdigest()
        release = {"tag_name":"v1.2.3", "assets":[{"name":"mihomo-linux-amd64-compatible-v1.2.3.gz",
                   "browser_download_url":"https://github.com/MetaCubeX/mihomo/releases/download/v1.2.3/core.gz", "digest":"sha256:"+digest}]}
        self.manager.core_path = self.manager.state_dir / 'bin' / 'mihomo'
        with mock.patch.object(core, '_architecture', return_value='amd64'), mock.patch.object(core, '_request', side_effect=[json.dumps(release).encode(), bytes(payload)]):
            installed = self.manager.install()
            self.assertTrue(installed['checksum_verified'])
            self.assertEqual(installed['version'], 'v1.2.3')
        release['assets'][0]['digest'] = 'sha256:' + '0'*64
        with mock.patch.object(core, '_architecture', return_value='amd64'), mock.patch.object(core, '_request', side_effect=[json.dumps(release).encode(), bytes(payload)]):
            with self.assertRaisesRegex(CoreError, 'SHA256 mismatch'):
                self.manager.install()

    def test_bad_ports_are_rejected(self):
        for port in (0, 80, 65536, True):
            with self.assertRaises(CoreError):
                self.manager.start(config={}, port=port)
        with self.assertRaises(CoreError):
            self.manager.start(config={}, port=7890, controller_port=7890)

    def test_mode_changes_live_and_survives_restart(self):
        self.start(mode='rule')
        for mode in ('global', 'direct', 'rule'):
            self.assertEqual(self.manager.mode(mode)['mode'], mode)
            self.assertEqual(self.manager._api('GET', '/configs')['mode'], mode)
        self.manager.mode('global')
        self.manager.stop()
        self.assertEqual(self.manager.controls()['mode'], 'global')
        self.start(mode='rule')
        self.assertEqual(self.manager.controls()['mode'], 'global')
        with self.assertRaisesRegex(CoreError, 'Proxy mode'):
            self.manager.mode('invalid')
        self.assertEqual(self.manager.controls()['mode'], 'global')

    def test_tun_requires_privileges_and_can_be_disabled_offline(self):
        with mock.patch.object(self.manager, 'tun_availability', return_value={'available': False, 'reason': 'no capability'}):
            with self.assertRaisesRegex(CoreError, 'no capability'):
                self.manager.tun(True)
            self.assertFalse(self.manager.tun(False)['tun'])
            with self.assertRaises(CoreError):
                self.manager.tun('on')
        with mock.patch.object(self.manager, 'tun_availability', return_value={'available': True, 'reason': ''}):
            self.manager.tun(True)
            self.start()
            self.assertTrue(self.manager.controls()['tun'])
            runtime = yaml.safe_load(self.manager.runtime_path.read_text())
            self.assertTrue(runtime['tun']['auto-route'])
            self.assertFalse(runtime['dns']['enable'])
            self.assertFalse(self.manager.tun(False)['tun'])

    def test_control_rollback_on_save_failure(self):
        from odcloud_sh.storage import State
        self.start(mode='rule')
        with mock.patch.object(State, 'write', side_effect=OSError('read-only')):
            with self.assertRaises(OSError):
                self.manager.mode('global')
        self.assertEqual(self.manager.controls()['mode'], 'rule')
        self.assertFalse((self.manager.state_dir / 'core-options.json').exists())

    def test_tun_keeps_system_dns_outside_proxy_udp_path(self):
        files = {
            '/etc/resolv.conf': 'nameserver 127.0.0.53\nnameserver 183.60.83.19\nnameserver bad\n',
            '/run/systemd/resolve/resolv.conf': 'nameserver 183.60.83.19\nnameserver 183.60.82.98\nnameserver ::1\n',
        }
        with mock.patch.object(Path, 'read_text', autospec=True, side_effect=lambda path: files[str(path)]):
            config = self.manager._tun_config(True)
        self.assertEqual(config['route-exclude-address'], ['183.60.82.98/32', '183.60.83.19/32'])
        self.assertTrue(config['auto-route'])
        self.assertEqual(config['dns-hijack'], [])
        self.assertNotIn('route-exclude-address', self.manager._tun_config(False))

    def test_control_rollback_on_verification_failure(self):
        self.start(mode='rule')
        original_api = self.manager._api
        reads = 0
        def api(method, path, payload=None):
            nonlocal reads
            result = original_api(method, path, payload)
            if method == 'GET' and path == '/configs':
                reads += 1
                if reads == 2:
                    return {'mode': 'rule'}
            return result
        with mock.patch.object(self.manager, '_api', side_effect=api):
            with self.assertRaisesRegex(CoreError, 'did not apply'):
                self.manager.mode('global')
        self.assertEqual(self.manager.controls()['mode'], 'rule')
        self.assertFalse((self.manager.state_dir / 'core-options.json').exists())


if __name__ == '__main__':
    unittest.main()

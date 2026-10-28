import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from odcloud_sh import core
from odcloud_sh.cli import parser


class CoreDownloadTests(unittest.TestCase):
    def fixture(self):
        payload = bytearray(64)
        payload[:6] = b'\x7fELF\x02\x01'
        payload[18:20] = (62).to_bytes(2, 'little')
        digest = hashlib.sha256(payload).hexdigest()
        page = f'''<ul><li><a href="/MetaCubeX/mihomo/releases/download/v1.2.3/mihomo-linux-amd64-compatible-v1.2.3.gz">core</a>
                   <span>sha256:{digest}</span></li>
                   <li><a href="/MetaCubeX/mihomo/releases/download/v1.2.3/mihomo-linux-arm64-v1.2.3.gz">other</a>
                   <span>sha256:{'a'*64}</span></li></ul>'''.encode()
        return bytes(payload), digest, page

    def test_api_rate_limit_falls_back_to_official_page_and_checks_digest(self):
        payload, digest, page = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            manager = core.CoreManager(directory)
            with patch.object(core, '_architecture', return_value='amd64'), patch.object(core, '_request', side_effect=[
                core.CoreError('HTTP 403'), (b'', core._RELEASE_ROOT + 'tag/v1.2.3'), page, payload
            ]) as download:
                result = manager.install()
            self.assertTrue(result['checksum_verified'])
            self.assertEqual(result['download_sha256'], digest)
            self.assertEqual(result['version'], 'v1.2.3')
            self.assertIn('/expanded_assets/v1.2.3', download.call_args_list[2].args[0])
            self.assertEqual(Path(result['path']).read_bytes(), payload)

    def test_pinned_page_does_not_need_latest_lookup(self):
        _, _, page = self.fixture()
        with patch.object(core, '_request', return_value=page) as request:
            result = core._release_from_page('v1.2.3')
        self.assertEqual(result['tag_name'], 'v1.2.3')
        self.assertEqual(request.call_count, 1)
        self.assertIn('expanded_assets/v1.2.3', request.call_args.args[0])

    def test_missing_digest_refuses_install_and_keeps_existing_core(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = core.CoreManager(directory)
            manager.core_path.parent.mkdir()
            manager.core_path.write_bytes(b'previous core')
            release = {'tag_name':'v1.2.3', 'assets':[{
                'name':'mihomo-linux-amd64-compatible-v1.2.3.gz',
                'browser_download_url':'https://github.com/MetaCubeX/mihomo/releases/download/v1.2.3/core.gz'}]}
            with patch.object(core, '_architecture', return_value='amd64'), patch.object(core, '_official_release', return_value=release):
                with self.assertRaisesRegex(core.CoreError, 'SHA256'):
                    manager.install()
            self.assertEqual(manager.core_path.read_bytes(), b'previous core')

    def test_latest_resolution_rejects_unexpected_release(self):
        with patch.object(core, '_request', return_value=(b'', 'https://example.test/tag/v1.2.3')):
            with self.assertRaises(core.CoreError):
                core._release_from_page('latest')

    def test_network_error_is_actionable_and_never_contains_tokens(self):
        with patch('requests.Session.send', side_effect=requests.exceptions.ProxyError('proxy-password secret-token')):
            with self.assertRaises(core.CoreError) as caught:
                core._request('https://github.com/example')
        self.assertIn('ProxyError', str(caught.exception))
        self.assertIn('--direct', str(caught.exception))
        self.assertNotIn('secret-token', str(caught.exception))

    def test_install_direct_flag(self):
        self.assertTrue(parser().parse_args(['core','install','--direct']).direct)


if __name__ == '__main__':
    unittest.main()

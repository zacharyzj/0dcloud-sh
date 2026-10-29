import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch

from odcloud_sh import autostart
from odcloud_sh.cli import main
from odcloud_sh.core import CoreManager
from odcloud_sh.errors import ClientError


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core = CoreManager(self.root / 'private state')
        self.core.core_path.parent.mkdir()
        self.core.core_path.write_bytes(b'fixture core')
        self.core.core_path.chmod(0o700)
        (self.core.state_dir / 'subscription.yaml').write_text('rules: []\n')
        for patcher in (patch.object(autostart, 'UNIT_PATH', self.root / autostart.SERVICE_NAME),
                        patch.object(autostart, '_require_systemd')):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(autostart, '_systemctl', side_effect=self.systemctl)
        self.systemctl_mock = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def systemctl(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1 if args[0] == 'is-enabled' else 0,
                                           'disabled\n' if args[0] == 'is-enabled' else '', '')

    def test_enable_generates_boot_unit_and_keeps_current_service_running(self):
        with patch.object(sys, 'executable', '/project/.venv/bin/python'):
            result = autostart.enable(self.core, group='Proxy', port=17890, controller_port=19090)
        self.assertTrue(result['enabled'])
        unit = autostart.UNIT_PATH.read_text()
        self.assertIn('Type=oneshot\nRemainAfterExit=yes', unit)
        self.assertIn('Wants=network-online.target', unit)
        self.assertIn('WantedBy=multi-user.target', unit)
        self.assertIn('"/project/.venv/bin/python"', unit)
        self.assertIn('"--state-dir" "' + str(self.core.state_dir) + '"', unit)
        self.assertIn('"connect" "--port" "17890" "--controller-port" "19090" "--group" "Proxy"', unit)
        self.assertIn('ExecStop=', unit)
        self.assertIn('"stop"', unit)
        self.assertNotIn('_0dcloud_pass', unit)
        self.assertEqual(stat.S_IMODE(autostart.UNIT_PATH.stat().st_mode), 0o644)
        self.assertEqual(self.systemctl_mock.call_args_list,
                         [call('is-enabled', autostart.SERVICE_NAME, check=False),
                          call('daemon-reload'), call('enable', autostart.SERVICE_NAME)])

    def test_enable_saves_actual_running_port_and_custom_core(self):
        custom = self.root / 'custom-core'
        custom.write_bytes(b'fixture core')
        custom.chmod(0o700)
        with patch.object(self.core, 'status', return_value={'running': True, 'proxy_port': 27890}), \
                patch.object(self.core, '_read_state', return_value={'core': str(custom)}):
            autostart.enable(self.core, port=17890)
        unit = autostart.UNIT_PATH.read_text()
        self.assertIn('"--port" "27890"', unit)
        self.assertIn('"--core" "' + str(custom) + '"', unit)

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze is unavailable')
    def test_generated_unit_passes_systemd_parser(self):
        autostart.enable(self.core, group='Hong Kong "$USER" %h \\ node')
        result = subprocess.run(['systemd-analyze', '--generators=no', '--man=no', '--recursive-errors=no',
                                 'verify', str(autostart.UNIT_PATH)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_disable_is_idempotent_and_does_not_stop_current_core(self):
        self.assertFalse(autostart.disable()['enabled'])
        self.systemctl_mock.assert_not_called()
        autostart.enable(self.core)
        self.systemctl_mock.reset_mock()
        self.assertFalse(autostart.disable()['enabled'])
        self.systemctl_mock.assert_called_once_with('disable', autostart.SERVICE_NAME)
        self.assertTrue(autostart.UNIT_PATH.exists())

    def test_failed_enable_restores_previous_unit(self):
        previous = autostart._MARKER.encode() + b'previous unit\n'
        for original in (None, previous):
            with self.subTest(original=original):
                if original is not None:
                    autostart.UNIT_PATH.write_bytes(original)
                def fail_enable(*args, **kwargs):
                    if args[0] == 'enable':
                        raise ClientError('enable failed')
                    return self.systemctl(*args, **kwargs)
                self.systemctl_mock.side_effect = fail_enable
                with self.assertRaisesRegex(ClientError, 'enable failed'):
                    autostart.enable(self.core)
                if original is None:
                    self.assertFalse(autostart.UNIT_PATH.exists())
                else:
                    self.assertEqual(autostart.UNIT_PATH.read_bytes(), original)

    def test_does_not_overwrite_unrelated_unit_or_symlink(self):
        autostart.UNIT_PATH.write_text('unrelated service\n')
        for function in (lambda: autostart.enable(self.core), autostart.disable):
            with self.assertRaises(ClientError):
                function()
        self.assertEqual(autostart.UNIT_PATH.read_text(), 'unrelated service\n')
        autostart.UNIT_PATH.unlink()
        autostart.UNIT_PATH.symlink_to(self.root / 'other-service')
        with self.assertRaises(ClientError):
            autostart.enable(self.core)
        self.systemctl_mock.assert_not_called()

    def test_unavailable_systemd_still_restores_previous_unit(self):
        previous = autostart._MARKER.encode() + b'previous unit\n'
        autostart.UNIT_PATH.write_bytes(previous)
        self.systemctl_mock.side_effect = [subprocess.CompletedProcess([], 1, 'disabled\n', ''),
                                          ClientError('reload failed'), ClientError('disable failed'),
                                          ClientError('reload failed again')]
        with self.assertRaisesRegex(ClientError, '^reload failed$'):
            autostart.enable(self.core)
        self.assertEqual(autostart.UNIT_PATH.read_bytes(), previous)

    def test_unsafe_values_or_missing_setup_do_not_write_unit(self):
        for options in ({'group': 'Proxy\nExecStart=bad'}, {'port': 80},
                        {'controller_port': 80}, {'port': 17890, 'controller_port': 17890}):
            with self.subTest(options=options), self.assertRaises(ClientError):
                autostart.enable(self.core, **options)
        (self.core.state_dir / 'subscription.yaml').unlink()
        with self.assertRaises(ClientError):
            autostart.enable(self.core)
        self.assertFalse(autostart.UNIT_PATH.exists())
        self.systemctl_mock.assert_not_called()

    def test_group_literals_are_escaped_for_systemd(self):
        autostart.enable(self.core, group='Hong Kong "$USER" %h \\ node')
        unit = autostart.UNIT_PATH.read_text()
        self.assertIn('"Hong Kong \\"$$USER\\" %%h \\\\ node"', unit)

    def test_status_distinguishes_enabled_disabled_and_not_found(self):
        for value, returncode, enabled in [('enabled', 0, True), ('disabled', 1, False),
                                           ('not-found', 4, False), ('enabled-runtime', 0, False)]:
            with self.subTest(value=value):
                self.systemctl_mock.side_effect = None
                self.systemctl_mock.return_value = subprocess.CompletedProcess([], returncode, value + '\n', '')
                result = autostart.status()
                self.assertEqual(result['enabled'], enabled)
                self.assertEqual(result['state'], value)

    def test_cli_enable_passes_startup_options(self):
        output = io.StringIO()
        with patch.object(autostart, 'enable', return_value={'enabled': True}) as enable:
            with contextlib.redirect_stdout(output):
                code = main(['--state-dir', str(self.core.state_dir), 'autostart', 'enable',
                             '--group', 'Proxy', '--port', '17890', '--controller-port', '19090'])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())['enabled'])
        self.assertEqual(enable.call_args.kwargs, {'group': 'Proxy', 'port': 17890, 'controller_port': 19090})


class SystemdRequirementsTests(unittest.TestCase):
    def test_requires_running_systemd_and_root_for_writes(self):
        with patch.object(autostart.platform, 'system', return_value='Linux'), \
                patch.object(autostart.shutil, 'which', return_value='/usr/bin/systemctl'), \
                patch.object(Path, 'is_dir', return_value=True), \
                patch.object(os, 'geteuid', return_value=1000):
            autostart._require_systemd()
            with self.assertRaisesRegex(ClientError, 'root'):
                autostart._require_systemd(writable=True)
        with patch.object(Path, 'is_dir', return_value=False):
            with self.assertRaisesRegex(ClientError, 'systemd'):
                autostart._require_systemd()

    def test_systemctl_failure_and_timeout_are_reported(self):
        failed = subprocess.CompletedProcess([], 1, '', 'failure')
        with patch.object(subprocess, 'run', return_value=failed):
            with self.assertRaises(ClientError):
                autostart._systemctl('enable', autostart.SERVICE_NAME)
        with patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('systemctl', 30)):
            with self.assertRaises(ClientError):
                autostart._systemctl('daemon-reload')


if __name__ == '__main__':
    unittest.main()

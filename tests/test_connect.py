import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from odcloud_sh.cli import main
from odcloud_sh.core import CoreError, CoreManager


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = CoreManager(self.temp.name)
        self.proxies = {
            'DIRECT': {'type': 'Direct'},
            'REJECT': {'type': 'Reject'},
            'GLOBAL': {'type': 'Selector', 'all': ['DIRECT', 'Proxy', 'one'], 'now': 'DIRECT'},
            'Other': {'type': 'Selector', 'all': ['one'], 'now': 'one'},
            'Proxy': {'type': 'Selector', 'all': ['DIRECT', 'REJECT', 'one', 'two'], 'now': 'two'},
            'one': {'type': 'Shadowsocks'},
            'two': {'type': 'AnyTLS'},
        }
        self.manager.runtime_path.write_text(yaml.safe_dump({'rules': ['MATCH,Proxy']}))
        for name, value in (('status', {'running': True, 'pid': 123}),
                            ('controls', {'mode': 'rule'}), ('nodes', self.proxies)):
            patcher = patch.object(self.manager, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.use = patch.object(self.manager, 'use').start()
        self.addCleanup(patch.stopall)

    def test_restored_selection_and_terminal_rule_take_priority(self):
        result = self.manager.connect()
        self.assertEqual((result['group'], result['node']), ('Proxy', 'two'))
        self.use.assert_not_called()

    def test_missing_or_direct_selection_falls_back_to_first_proxy(self):
        for current in ('removed', 'DIRECT', 'REJECT', None):
            with self.subTest(current=current):
                self.proxies['Proxy']['now'] = current
                self.assertEqual(self.manager.connect()['node'], 'one')
                self.use.assert_called_with('Proxy', 'one')

    def test_nested_groups_avoid_cycles_and_select_from_inside_out(self):
        self.proxies['Proxy'] = {'type': 'Selector', 'all': ['DIRECT', 'Nested'], 'now': 'DIRECT'}
        self.proxies['Nested'] = {'type': 'Selector', 'all': ['Proxy', 'two'], 'now': 'Proxy'}
        self.assertEqual(self.manager.connect()['node'], 'two')
        self.assertEqual(self.use.call_args_list, [unittest.mock.call('Nested', 'two'),
                                                  unittest.mock.call('Proxy', 'Nested')])

    def test_global_mode_keeps_its_cached_node(self):
        self.manager.controls.return_value = {'mode': 'global'}
        self.proxies['GLOBAL']['now'] = 'one'
        self.assertEqual(self.manager.connect()['node'], 'one')
        self.use.assert_not_called()

    def test_global_mode_can_follow_a_restored_nested_selection(self):
        self.manager.controls.return_value = {'mode': 'global'}
        result = self.manager.connect()
        self.assertEqual((result['group'], result['node']), ('GLOBAL', 'two'))
        self.use.assert_called_once_with('GLOBAL', 'Proxy')

    def test_explicit_group_is_used_by_global_mode(self):
        self.manager.controls.return_value = {'mode': 'global'}
        self.assertEqual(self.manager.connect(group='Proxy')['node'], 'two')
        self.use.assert_called_once_with('GLOBAL', 'Proxy')
        with self.assertRaises(CoreError):
            self.manager.connect(group='Other')

    def test_default_group_skips_groups_without_proxy_nodes(self):
        self.manager.runtime_path.write_text(yaml.safe_dump({'rules': ['MATCH,DIRECT']}))
        self.proxies['Other'] = {'type': 'Selector', 'all': ['DIRECT'], 'now': 'DIRECT'}
        self.assertEqual(self.manager.connect()['group'], 'Proxy')

    def test_direct_mode_is_enabled_only_after_a_proxy_is_selected(self):
        self.manager.controls.return_value = {'mode': 'direct'}
        with patch.object(self.manager, 'mode') as mode:
            self.manager.connect()
            mode.assert_called_once_with('rule')
        self.proxies['Proxy']['all'] = ['DIRECT', 'REJECT']
        with patch.object(self.manager, 'mode') as mode:
            with self.assertRaises(CoreError):
                self.manager.connect()
            mode.assert_not_called()

    def test_explicit_group_and_no_usable_proxy(self):
        self.assertEqual(self.manager.connect(group='Other')['node'], 'one')
        self.proxies['Proxy']['all'] = ['DIRECT', 'REJECT', 'removed']
        for group in ('Proxy', 'missing'):
            with self.subTest(group=group), self.assertRaises(CoreError):
                self.manager.connect(group=group)
        self.use.assert_not_called()

    def test_cli_connect_passes_options(self):
        output = io.StringIO()
        with patch.object(CoreManager, 'connect', return_value={'node': 'two'}) as connect:
            with contextlib.redirect_stdout(output):
                result = main(['--state-dir', self.temp.name, 'connect', '--group', 'Proxy',
                               '--port', '17890', '--controller-port', '19090'])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), {'node': 'two'})
        connect.assert_called_once_with(Path(self.temp.name) / 'subscription.yaml', port=17890,
                                        controller_port=19090, group='Proxy')


class StartProxyScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project with spaces'
        self.root.mkdir()
        source = Path(__file__).resolve().parents[1] / 'start-proxy.sh'
        shutil.copy2(source, self.root / 'start-proxy.sh')
        python = self.root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        (self.root / 'proxyctl').write_text('''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path('calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
if os.environ.get('FAIL_CONNECT') and 'connect' in args:
    raise SystemExit(1)
if os.environ.get('FAIL_AUTOSTART') and 'autostart' in args:
    raise SystemExit(1)
if 'env' in args:
    print("export HTTP_PROXY=http://127.0.0.1:17890;")
    print("export https_proxy=http://127.0.0.1:17890;")
else:
    print('{"node": "two"}')
''')
        self.env = {key: value for key, value in os.environ.items()
                    if not key.lower().endswith('_proxy')}

    def run_shell(self, shell, command, *args, env=None):
        return subprocess.run([shell, '-c', command, 'test-shell', *args],
                              cwd=self.temp.name, env=env or self.env, text=True,
                              capture_output=True, timeout=10)

    def test_source_sets_current_shell_and_keeps_working_directory(self):
        for shell in ('bash', 'zsh'):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                result = self.run_shell(shell, '''
set -eu
before="$PWD"
source "$1" --state-dir "$2" --group Proxy --port 17890
test "$PWD" = "$before"
test "$HTTP_PROXY" = http://127.0.0.1:17890
test "$https_proxy" = "$HTTP_PROXY"
"$3" -c 'import os; print("HTTP_PROXY=" + os.environ["HTTP_PROXY"])'
''', str(self.root / 'start-proxy.sh'), str(self.root / 'private state'), sys.executable)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'HTTP_PROXY=http://127.0.0.1:17890')
                self.assertIn('"node": "two"', result.stderr)
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls[0], ['--state-dir', str(self.root / 'private state'),
                                   'connect', '--group', 'Proxy', '--port', '17890'])
        self.assertEqual(calls[1], ['--state-dir', str(self.root / 'private state'), 'env'])

    def test_executed_script_outputs_commands_that_can_be_evaluated(self):
        result = self.run_shell('bash', 'eval "$("$1" --group Proxy)"; test "$HTTP_PROXY" = http://127.0.0.1:17890',
                                str(self.root / 'start-proxy.sh'))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failure_does_not_set_environment_or_exit_the_callers_shell(self):
        for shell in ('bash', 'zsh'):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                result = self.run_shell(shell, '''
export HTTP_PROXY=previous
if source "$1"; then exit 3; fi
test "$HTTP_PROXY" = previous
''', str(self.root / 'start-proxy.sh'), env={**self.env, 'FAIL_CONNECT': '1'})
                self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        self.assertTrue(all('env' not in args for args in calls))

    def test_help_does_not_start_core(self):
        result = self.run_shell('bash', 'source "$1" --help', str(self.root / 'start-proxy.sh'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'calls.jsonl').exists())

    def test_enable_autostart_registers_only_after_successful_connect(self):
        for shell in ('bash', 'zsh'):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                result = self.run_shell(shell, 'source "$1" --enable-autostart --state-dir "$2" --group Proxy --port 17890',
                                        str(self.root / 'start-proxy.sh'), str(self.root / 'state'))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '')
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        base = ['--state-dir', str(self.root / 'state')]
        self.assertEqual(calls[:3], [base + ['connect', '--group', 'Proxy', '--port', '17890'],
                                    base + ['autostart', 'enable', '--group', 'Proxy', '--port', '17890'],
                                    base + ['env']])

    def test_autostart_management_does_not_connect_or_change_shell_proxy(self):
        for shell in ('bash', 'zsh'):
            if not shutil.which(shell):
                continue
            for option in ('--disable-autostart', '--autostart-status'):
                with self.subTest(shell=shell, option=option):
                    result = self.run_shell(shell, '''
export HTTP_PROXY=previous
source "$1" "$2"
test "$HTTP_PROXY" = previous
''', str(self.root / 'start-proxy.sh'), option)
                    self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls[:2], [['autostart', 'disable'], ['autostart', 'status']])
        self.assertTrue(all(args[0] == 'autostart' for args in calls))

    def test_connect_failure_does_not_register_autostart(self):
        result = self.run_shell('bash', 'source "$1" --enable-autostart', str(self.root / 'start-proxy.sh'),
                                env={**self.env, 'FAIL_CONNECT': '1'})
        self.assertNotEqual(result.returncode, 0)
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls, [['connect']])

    def test_autostart_failure_is_reported_without_setting_proxy_variables(self):
        result = self.run_shell('bash', '''
export HTTP_PROXY=previous
if source "$1" --enable-autostart; then exit 3; fi
test "$HTTP_PROXY" = previous
''', str(self.root / 'start-proxy.sh'), env={**self.env, 'FAIL_AUTOSTART': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls, [['connect'], ['autostart', 'enable']])

    def test_conflicting_autostart_options_do_nothing(self):
        result = self.run_shell('bash', 'source "$1" --enable-autostart --disable-autostart',
                                str(self.root / 'start-proxy.sh'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'calls.jsonl').exists())


if __name__ == '__main__':
    unittest.main()

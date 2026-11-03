"""Exercise desktop switching with real KConfig tools and an isolated service manager."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


COMMAND = Path(__file__).resolve().parents[1] / 'kosctl-switch'
UNITS = ('kos-platform.service', 'kos-data.service', 'kos-shell.service')
SERVICE_MANAGER = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

root = Path(os.environ['KOS_SWITCH_TEST_ROOT'])
if Path(sys.argv[0]).name == 'busctl':
    sys.exit(0)
path = root / 'services.json'
states = json.loads(path.read_text())
args = sys.argv[1:]
action = args[1]
units = [arg for arg in args[2:] if arg.endswith('.service')]
if action == 'show':
    print('loaded' if units[0] in states else 'not-found')
elif action == 'is-enabled':
    print(states[units[0]]['enabled'])
elif action == 'is-active':
    sys.exit(0 if states[units[0]]['active'] else 3)
elif action in ('enable', 'disable', 'start', 'stop'):
    for unit in units:
        if action == 'enable':
            states[unit]['enabled'] = 'enabled-runtime' if '--runtime' in args else 'enabled'
        elif action == 'disable':
            states[unit]['enabled'] = 'disabled'
        if action in ('start', 'stop') or '--now' in args:
            states[unit]['active'] = action in ('enable', 'start')
        path.write_text(json.dumps(states))
        if action == 'enable' and (root / 'fail-start').exists():
            (root / 'fail-start').unlink()
            print('Simulated service start failure', file=sys.stderr)
            sys.exit(1)
'''


@unittest.skipUnless(shutil.which('kwriteconfig6') and shutil.which('kreadconfig6'),
                     'KConfig command-line tools are required')
class DesktopSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        binaries = self.root / 'bin'
        binaries.mkdir()
        for name in ('systemctl', 'busctl'):
            script = binaries / name
            script.write_text(SERVICE_MANAGER)
            script.chmod(0o755)
        theme = self.root / 'data/plasma/shells/org.kos.desktop/contents'
        theme.mkdir(parents=True)
        (theme / 'defaults').write_text('[Desktop]\nContainment=org.kde.desktopcontainment\n')
        self.original = {
            'kwinrc': '[Plugins]\nblurEnabled=true\n\n[Other]\nCustom=keep me\n',
            'plasmashellrc': '[Shell]\nShellPackage=org.kde.plasma.desktop\n',
        }
        for name, content in self.original.items():
            (self.config / name).write_text(content)
        self.services = self.root / 'services.json'
        self.services.write_text(json.dumps({unit: {'enabled': 'disabled', 'active': False}
                                             for unit in UNITS}))
        self.env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
                    'XDG_CONFIG_HOME': str(self.config), 'XDG_STATE_HOME': str(self.root / 'state'),
                    'XDG_DATA_HOME': str(self.root / 'data'), 'XDG_DATA_DIRS': str(self.root / 'data'),
                    'KOS_SWITCH_TEST_ROOT': str(self.root)}

    def command(self, argument, success=True):
        result = subprocess.run([str(COMMAND), argument], env=self.env,
                                text=True, capture_output=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def assert_restored(self):
        for name, content in self.original.items():
            self.assertEqual((self.config / name).read_text(), content)

    def test_enable_starts_services_and_disable_restores_configuration(self):
        self.command('enable')
        self.assertIn('ShellPackage=org.kos.desktop', (self.config / 'plasmashellrc').read_text())
        self.assertIn('library=kos_decoration', (self.config / 'kwinrc').read_text())
        self.assertTrue(all(s['active'] and s['enabled'] == 'enabled'
                            for s in json.loads(self.services.read_text()).values()))
        self.command('disable')
        self.assert_restored()
        self.assertTrue(all(not s['active'] and s['enabled'] == 'disabled'
                            for s in json.loads(self.services.read_text()).values()))
        self.command('disable')
        self.assert_restored()

    def test_repeated_enable_preserves_the_first_backup(self):
        self.command('enable')
        with (self.config / 'kwinrc').open('a') as stream:
            stream.write('\n[ChangedWhileEnabled]\nvalue=temporary\n')
        self.command('enable')
        self.command('disable')
        self.assert_restored()
        (self.config / 'kwinrc').write_text('[NewSession]\nvalue=preserved\n')
        self.command('enable')
        self.command('disable')
        self.assertEqual((self.config / 'kwinrc').read_text(), '[NewSession]\nvalue=preserved\n')

    def test_absent_configuration_is_absent_after_disable(self):
        for name in self.original:
            (self.config / name).unlink()
        self.command('enable')
        self.command('disable')
        self.assertTrue(all(not (self.config / name).exists() for name in self.original))

    def test_symlinked_configuration_restores_contents_and_link(self):
        target = self.root / 'dotfiles/kwinrc'
        target.parent.mkdir()
        (self.config / 'kwinrc').rename(target)
        (self.config / 'kwinrc').symlink_to('../dotfiles/kwinrc')
        self.command('enable')
        self.command('disable')
        self.assert_restored()
        self.assertTrue((self.config / 'kwinrc').is_symlink())
        self.assertEqual(target.read_text(), self.original['kwinrc'])

    def test_failed_start_rolls_back_configuration_and_previous_service_state(self):
        previous = json.loads(self.services.read_text())
        previous[UNITS[0]] = {'enabled': 'enabled-runtime', 'active': True}
        self.services.write_text(json.dumps(previous))
        (self.root / 'fail-start').touch()
        self.command('enable', success=False)
        self.assert_restored()
        self.assertEqual(json.loads(self.services.read_text()), previous)

    def test_missing_service_does_not_change_configuration(self):
        states = json.loads(self.services.read_text())
        del states[UNITS[0]]
        self.services.write_text(json.dumps(states))
        self.command('enable', success=False)
        self.assert_restored()

    def test_disable_restores_configuration_after_unit_files_are_removed(self):
        self.command('enable')
        self.services.write_text('{}')
        self.command('disable')
        self.assert_restored()

    def test_only_enable_and_disable_are_accepted(self):
        self.command('install', success=False)
        self.command('update', success=False)
        self.assert_restored()


if __name__ == '__main__':
    # CI containers run as root; exercise the public command as a real non-root
    # desktop user without adding a privilege bypass to the installed tool.
    if os.geteuid() == 0:
        import pwd
        account = pwd.getpwnam('nobody')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            (root / 'tools/tests').mkdir(parents=True)
            shutil.copy2(COMMAND, root / 'tools/kosctl-switch')
            script = root / 'tools/tests/test_kosctl_switch.py'
            shutil.copy2(__file__, script)
            result = subprocess.run([sys.executable, str(script)], cwd=root,
                                    user=account.pw_uid, group=account.pw_gid, extra_groups=[])
        sys.exit(result.returncode)
    unittest.main()

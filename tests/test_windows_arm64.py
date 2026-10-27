"""Exercise the Windows install script in PowerShell with a fixture MSI process."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / '.github/workflows/windows-arm64.yml').read_text(), Loader=yaml.BaseLoader)
STEP = next(step for step in DATA['jobs']['install']['steps'] if 'run' in step)

# The fixture creates files at independently calculated expected locations. Its
# CLI is runnable on POSIX and carries the machine bytes inspected by the script.
INSTALLER = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open('processes.jsonl', 'a') as log:
    log.write(json.dumps(args) + '\n')
if args[0] == '/i':
    Path('install.log').touch()
    launcher = os.environ['TEST_PER_USER'] == 'true'
    scope = os.environ['TEST_SCOPE']
    per_user = scope == 'perUser' or (not scope and launcher)
    expected = ['ALLUSERS=2', 'MSIINSTALLPERUSER=1'] if scope == 'perUser' else ['ALLUSERS=1', 'MSIINSTALLPERUSER=""'] if scope else []
    if any(value not in args for value in expected):
        raise SystemExit('explicit install scope was not passed to msiexec')
    relative = os.environ['TEST_INSTALL_PATH'] or ('Programs/' if per_user else '') + os.environ['TEST_APP_DIR']
    directory = Path(os.environ['LOCALAPPDATA'] if per_user else os.environ['ProgramFiles'])
    directory = directory.joinpath(*relative.replace(chr(92), '/').split('/'))
    custom = next((arg for arg in args if arg.startswith('INSTALLFOLDER=')), None)
    if custom:
        directory = Path(custom[len('INSTALLFOLDER='):].strip('"').rstrip('\\'))
    directory.mkdir(parents=True)
    names = [os.environ['TEST_MAIN_BINARY']]
    if not launcher:
        names.append(os.environ['TEST_CLI_BINARY'])
    files = [directory / name for name in set(names)]
    for path in files:
        data = bytearray(b'#!/bin/sh\necho "fixture 1.0"\ntouch cli-ran\nexit 0\n'.ljust(80, b'\x00'))
        data[0x3c:0x40] = (64).to_bytes(4, 'little')
        data[68:70] = (0xaa64).to_bytes(2, 'little')
        path.write_bytes(data)
        path.chmod(0o755)
    if launcher:
        for folder in ('programs', 'desktop'):
            if folder == 'desktop' and 'INSTALLDESKTOPSHORTCUT=0' in args:
                continue
            shortcut = Path(folder) / (os.environ['TEST_APP_DIR'] + '.lnk')
            shortcut.touch()
            files.append(shortcut)
    Path('installed.json').write_text(json.dumps([str(path) for path in files]))
    Path('directory.txt').write_text(str(directory))
else:
    for path in json.loads(Path('installed.json').read_text()):
        Path(path).unlink()
    Path(Path('directory.txt').read_text()).rmdir()
'''

PRELUDE = r'''
function Start-Process {
  param($FilePath, [string[]] $ArgumentList, [switch] $Wait, [switch] $PassThru)
  if ($FilePath -ne 'msiexec.exe') { throw "unexpected process $FilePath" }
  & $env:TEST_PYTHON fixture.py @ArgumentList
  if ($LASTEXITCODE -ne 0) { throw 'fixture installer failed' }
  [pscustomobject]@{ ExitCode = 0 }
}
function Get-ItemProperty {
  param([string] $LiteralPath, $ErrorAction)
  if ($LiteralPath -ne "HKCU:\Software\Learning Machines LLC\$env:TEST_APP_DIR") {
    throw "unexpected registry key $LiteralPath"
  }
  [pscustomobject]@{ InstallDir = $null }
}
'''


@unittest.skipUnless(shutil.which('pwsh') and os.name == 'posix', 'requires PowerShell on POSIX')
class WindowsArm64Tests(unittest.TestCase):
    def run_install(self, per_user=False, **overrides):
        inputs = {'app-name': 'pdfcraft', 'app-dir': 'PDFCraft',
                  'main-binary': '', 'cli-binary': '', 'is-per-user': str(per_user).lower(),
                  'install-relative-path': '', 'install-scope': '',
                  **overrides}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ('dist', 'local', 'program-files', 'programs', 'desktop'):
                (root / folder).mkdir()
            (root / 'dist/fixture.msi').touch()
            (root / 'fixture.py').write_text(INSTALLER)
            env = {**os.environ, 'LOCALAPPDATA': str(root / 'local'),
                   'ProgramFiles': str(root / 'program-files'), 'TEST_PYTHON': sys.executable,
                   'TEST_PER_USER': inputs['is-per-user'], 'TEST_APP_DIR': inputs['app-dir'],
                   'TEST_SCOPE': inputs['install-scope'], 'TEST_INSTALL_PATH': inputs['install-relative-path'],
                   'TEST_MAIN_BINARY': inputs['main-binary'] or inputs['app-name'] + '.exe',
                   'TEST_CLI_BINARY': inputs['cli-binary'] or inputs['app-name'] + '-cli.exe'}
            script = STEP['run']
            configured = dict(STEP.get('env', {}))
            for name, value in inputs.items():
                expression = '${{ inputs.' + name + ' }}'
                # Model GitHub interpolation in both source and env, so a
                # regression to source interpolation exposes the payload.
                script = script.replace(expression, value)
                configured = {key: item.replace(expression, value) for key, item in configured.items()}
            env.update(configured)
            # Only substitute native special-folder APIs; leave input handling,
            # path operations, architecture checks and CLI invocation intact.
            for folder in ('Programs', 'Desktop'):
                script = script.replace(f"([Environment]::GetFolderPath('{folder}'))",
                                        f"'{root / folder.lower()}'")
            (root / 'install.ps1').write_text(PRELUDE + script)
            result = subprocess.run(['pwsh', '-NoLogo', '-NoProfile', '-File', 'install.ps1'],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=20)
            log = root / 'processes.jsonl'
            processes = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, processes, (root / 'injected').exists(), (root / 'cli-ran').exists()

    def test_defaults_and_custom_names_in_both_branches(self):
        for per_user in (False, True):
            for inputs in ({}, {'app-name': 'craft-launcher', 'app-dir': 'ArtCraft Launcher',
                               'main-binary': 'artcraft-launcher.exe', 'cli-binary': 'artcraft-cli.exe'},
                           {'main-binary': 'same.exe', 'cli-binary': 'same.exe'}):
                with self.subTest(per_user=per_user, inputs=inputs):
                    result, processes, injected, cli_ran = self.run_install(per_user, **inputs)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual([args[0] for args in processes],
                                     ['/i', '/x', '/i', '/x'] if per_user else ['/i', '/x'])
                    self.assertFalse(injected)
                    self.assertEqual(cli_ran, not per_user)

    def test_explicit_context_and_nested_path_reach_the_installer(self):
        for launcher, scope, relative in (
            (False, 'perUser', 'Programs/ArtCraft/PhotoCraft'),
            (False, 'perMachine', 'ArtCraft/PhotoCraft'),
            (True, 'perUser', 'Programs/ArtCraft/Launcher'),
        ):
            with self.subTest(launcher=launcher, scope=scope):
                result, processes, injected, cli_ran = self.run_install(
                    launcher, **{'install-scope': scope, 'install-relative-path': relative})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([args[0] for args in processes],
                                 ['/i', '/x', '/i', '/x'] if launcher else ['/i', '/x'])
                self.assertEqual(cli_ran, not launcher)
                self.assertFalse(injected)

    def test_powershell_syntax_in_names_stays_literal(self):
        for value in ("Craft'; New-Item injected; #", 'Craft$(New-Item injected)',
                      'Craft`$(New-Item injected)', "Craft's [ARM64] é"):
            for name in ('app-name', 'app-dir', 'main-binary', 'cli-binary'):
                for per_user in (False, True):
                    with self.subTest(name=name, value=value, per_user=per_user):
                        result, _, injected, _ = self.run_install(per_user, **{name: value})
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertFalse(injected)

    def test_invalid_names_fail_before_installing(self):
        for value in ('../outside', '..\\outside', 'C:outside', 'C:\\outside', '\\\\host\\share',
                      '.', '..', 'trailing.', 'trailing ', 'bad*', 'bad?', 'bad|', 'bad<', 'bad>',
                      'bad\x01', 'bad\nNew-Item injected', 'bad"; New-Item injected; #'):
            for name in ('app-name', 'app-dir', 'main-binary', 'cli-binary'):
                # app-name receives a suffix when used as a default filename.
                if name == 'app-name' and value in ('.', '..', 'trailing.', 'trailing '):
                    continue
                for per_user in (False, True):
                    with self.subTest(name=name, value=value, per_user=per_user):
                        result, processes, injected, _ = self.run_install(per_user, **{name: value})
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(processes, [])
                        self.assertFalse(injected)
        result, processes, _, _ = self.run_install(**{'app-dir': ''})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(processes, [])


if __name__ == '__main__':
    unittest.main()

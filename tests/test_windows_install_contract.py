"""Run the workflow's real input resolution without starting an installer."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / '.github/workflows/windows-arm64.yml').read_text(), Loader=yaml.BaseLoader)
STEP = next(step for step in DATA['jobs']['install']['steps'] if 'run' in step)
SHELL = shutil.which('pwsh') or shutil.which('powershell')


@unittest.skipUnless(SHELL, 'requires PowerShell')
class InstallContractTests(unittest.TestCase):
    def resolve(self, relative='', scope='', launcher=False, app='PhotoCraft'):
        """Resolve only the workflow input contract without invoking msiexec."""
        inputs = {'app-name': 'photocraft', 'app-dir': app, 'main-binary': 'photocraft.exe',
                  'cli-binary': 'photocraft-cli.exe', 'is-per-user': str(launcher).lower(),
                  'install-relative-path': relative, 'install-scope': scope}
        env = {key: value for key, value in os.environ.items()
               if key.lower() not in ('localappdata', 'programfiles')}
        for name, expression in STEP['env'].items():
            for key, value in inputs.items():
                expression = expression.replace('${{ inputs.' + key + ' }}', value)
            env[name] = expression
        with tempfile.TemporaryDirectory() as directory:
            env['LOCALAPPDATA'] = str(Path(directory) / 'local')
            env['ProgramFiles'] = str(Path(directory) / 'machine')
            env['TEST_PROGRAM_FILES'] = env['ProgramFiles']
            prefix = STEP['run'].split('$msi =', 1)[0]
            # Windows PowerShell resets ProgramFiles for its own process architecture.
            script = '$env:ProgramFiles = $env:TEST_PROGRAM_FILES\n' + prefix + '\n@{directory=$installDirectory; properties=@($scopeProperties); perUser=$perUser} | ConvertTo-Json -Compress\n'
            file = Path(directory) / 'contract.ps1'
            file.write_text(script, encoding='utf-8')
            result = subprocess.run([SHELL, '-NoProfile', '-File', str(file)], env=env,
                                    capture_output=True, text=True, timeout=20)
            data = json.loads(result.stdout) if result.returncode == 0 else None
            return result, data, env

    def test_nested_per_user_path_and_explicit_scope(self):
        """Nested per user path and explicit scope."""
        result, data, env = self.resolve('Programs/ArtCraft/PhotoCraft', 'perUser')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsInstance(data['directory'], str, 'The workflow must resolve the requested install directory.')
        self.assertEqual(Path(data['directory']), Path(env['LOCALAPPDATA']) / 'Programs/ArtCraft/PhotoCraft')
        self.assertTrue(data['perUser'])
        self.assertEqual(data['properties'], ['ALLUSERS=2', 'MSIINSTALLPERUSER=1'])

    def test_machine_scope_and_legacy_defaults(self):
        """Machine scope and legacy defaults."""
        for launcher, scope, relative, suffix, user in (
            (False, '', '', 'PhotoCraft', False),
            (True, '', '', 'Programs/PhotoCraft', True),
            (False, 'perMachine', 'ArtCraft/PhotoCraft', 'ArtCraft/PhotoCraft', False),
        ):
            with self.subTest(launcher=launcher, scope=scope):
                result, data, env = self.resolve(relative, scope, launcher)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(Path(data['directory']), Path(env['LOCALAPPDATA' if user else 'ProgramFiles']) / suffix)
                self.assertEqual(data['perUser'], user)
                self.assertEqual(data['properties'], [] if not scope else ['ALLUSERS=1', 'MSIINSTALLPERUSER=""'])

    def test_invalid_relative_paths_and_scope_fail_before_install(self):
        """Invalid relative paths and scope fail before install."""
        for relative in ('../outside', 'ArtCraft/../outside', '/absolute', 'C:relative',
                         'C:/absolute', '//host/share', 'ArtCraft//PhotoCraft',
                         'ArtCraft/./PhotoCraft', 'ArtCraft/trailing.', 'ArtCraft/trailing ',
                         'ArtCraft/bad*', 'ArtCraft/bad"', 'ArtCraft/bad\nname'):
            with self.subTest(relative=relative):
                result, _, _ = self.resolve(relative, 'perUser')
                self.assertNotEqual(result.returncode, 0)
        result, _, _ = self.resolve('ArtCraft/PhotoCraft', 'invalid')
        self.assertNotEqual(result.returncode, 0)
        result, _, _ = self.resolve('', 'perMachine', True)
        self.assertNotEqual(result.returncode, 0)

    def test_path_metacharacters_stay_literal(self):
        """Path metacharacters stay literal."""
        for name in ("Craft's [ARM64]", 'Craft$(Write-Output injected)', 'Craft`name'):
            with self.subTest(name=name):
                result, data, env = self.resolve('Programs/ArtCraft/' + name, 'perUser')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(Path(data['directory']), Path(env['LOCALAPPDATA']) / 'Programs/ArtCraft' / name)


if __name__ == '__main__':
    unittest.main()

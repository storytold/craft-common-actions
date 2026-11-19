"""The platform-leg selector, run on a small git repository with a stand-in cargo."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION_DIR = ROOT / 'actions/select-platforms'
SCRIPT = ACTION_DIR / 'select_platforms.py'
GATE = yaml.load((ROOT / '.github/workflows/gate.yml').read_text(), Loader=yaml.BaseLoader)

sys.path.insert(0, str(ACTION_DIR))
from select_platforms import has_platform_cfg  # noqa: E402

EVERY = '["ubuntu-24.04","windows-latest","macos-latest"]'
SELECT = '["windows-latest","macos-latest"]'
LINUX = ['ubuntu-24.04']
ALL = json.loads(EVERY)

CRATES = {
    # name: (source, dependencies)
    'leaf': ('pub fn f() {}\n', []),
    'plat': ('#[cfg(windows)]\npub fn f() {}\n', []),
    'user': ('pub fn g() {}\n', ['plat']),
    'web': ('#[cfg(not(target_family = "wasm"))]\npub fn f() {}\n', ['leaf']),
}


class SelectPlatformsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name, 'repo')
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'test')
        self.write('Cargo.toml', '[workspace]\nmembers = ["crates/*"]\n')
        self.write('Cargo.lock', 'version = 4\n')
        packages = []
        for name, (source, deps) in CRATES.items():
            self.write(f'crates/{name}/Cargo.toml', f'[package]\nname = "{name}"\n')
            self.write(f'crates/{name}/src/lib.rs', source)
            packages.append({'name': name, 'id': f'path+file://{name}',
                             'manifest_path': str(self.repo / f'crates/{name}/Cargo.toml'),
                             'dependencies': [{'name': d, 'kind': None, 'path': str(self.repo / f'crates/{d}')}
                                              for d in deps]})
        metadata = {'packages': packages, 'workspace_members': [p['id'] for p in packages]}
        bin_dir = Path(self.temp.name, 'bin')
        bin_dir.mkdir()
        Path(self.temp.name, 'metadata.json').write_text(json.dumps(metadata))
        cargo = bin_dir / 'cargo'
        cargo.write_text(f'#!/bin/sh\ncat "{self.temp.name}/metadata.json"\n')
        cargo.chmod(0o755)
        self.path = f'{bin_dir}:{os.environ["PATH"]}'
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'base')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True)

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def change(self, path):
        self.write(path, (self.repo / path).read_text() + '// changed\n' if (self.repo / path).exists() else 'new\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'change')

    def select(self, event='pull_request', labels=(), crates='', paths='', select=SELECT):
        output = Path(self.temp.name, 'output')
        output.write_text('')
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--os', EVERY, '--select-os', select, '--label', 'full-ci',
             '--crates', crates, '--paths', paths],
            cwd=self.repo, capture_output=True, text=True,
            env={**os.environ, 'PATH': self.path, 'EVENT': event, 'LABELS': json.dumps(list(labels)),
                 'GITHUB_OUTPUT': str(output)})
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = dict(line.split('=', 1) for line in output.read_text().splitlines())
        return json.loads(fields['os'])

    def test_a_crate_without_platform_code_runs_linux_only(self):
        self.change('crates/leaf/src/lib.rs')
        self.assertEqual(self.select(), LINUX)

    def test_a_wasm_only_cfg_is_not_platform_code(self):
        self.change('crates/web/src/lib.rs')
        self.assertEqual(self.select(), LINUX)

    def test_a_crate_with_platform_code_runs_every_leg(self):
        self.change('crates/plat/src/lib.rs')
        self.assertEqual(self.select(), ALL)

    def test_a_crate_that_depends_on_platform_code_runs_every_leg(self):
        self.change('crates/user/src/lib.rs')
        self.assertEqual(self.select(), ALL)

    def test_the_build_setup_runs_every_leg(self):
        for path in ('Cargo.lock', 'Cargo.toml', '.github/workflows/workspace.yml', '.config/nextest.toml'):
            with self.subTest(path=path):
                self.change(path)
                self.assertEqual(self.select(), ALL)

    def test_a_file_outside_every_crate_runs_every_leg_unless_it_is_documentation(self):
        self.change('vendor/lib/src/lib.rs')
        self.assertEqual(self.select(), ALL)
        self.change('docs/guide.md')
        self.assertEqual(self.select(), LINUX)

    def test_extra_paths_and_crates_count(self):
        self.change('crates/leaf/src/lib.rs')
        self.assertEqual(self.select(crates='leaf'), ALL)
        self.assertEqual(self.select(crates='web'), LINUX)
        self.assertEqual(self.select(paths='crates/leaf/*'), ALL)

    def test_the_label_and_other_events_run_every_leg(self):
        self.change('crates/leaf/src/lib.rs')
        self.assertEqual(self.select(labels=['full-ci']), ALL)
        self.assertEqual(self.select(event='push'), ALL)
        self.assertEqual(self.select(event='schedule'), ALL)

    def test_select_os_must_leave_a_leg_and_name_known_legs(self):
        for select in (EVERY, '["freebsd"]'):
            with self.subTest(select=select):
                result = subprocess.run([sys.executable, str(SCRIPT), '--os', EVERY, '--select-os', select],
                                        cwd=self.repo, capture_output=True, text=True,
                                        env={**os.environ, 'EVENT': 'pull_request'})
                self.assertNotEqual(result.returncode, 0)


class PlatformCfgTests(unittest.TestCase):
    def test_predicates(self):
        cases = {
            '#[cfg(windows)]': True,
            '#[cfg(all(feature = "x", not(unix)))]': True,
            'if cfg!(target_os = "macos") {}': True,
            '#[cfg_attr(target_vendor = "apple", path = "mac.rs")]': True,
            "[target.'cfg(windows)'.dependencies]": True,
            '#[cfg(target_family = "unix")]': True,
            '#[cfg(not(target_family = "wasm"))]': False,
            '#[cfg(target_arch = "wasm32")]': False,
            '#[cfg(feature = "egui")]\nfn windows() {}': False,
            'my_cfg(windows)': False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(has_platform_cfg(text), expected)


class GateSelectJobTests(unittest.TestCase):
    def test_every_job_reads_its_legs_from_the_select_job_or_os(self):
        for name in ('build', 'lint', 'gate'):
            with self.subTest(job=name):
                job = GATE['jobs'][name]
                self.assertIn('select', job['needs'])
                self.assertEqual(job['strategy']['matrix']['os'], '${{ fromJSON(needs.select.outputs.os || inputs.os) }}')
                self.assertTrue(job['if'].startswith("${{ !cancelled() && needs.select.result != 'failure'"), job['if'])

    def test_select_runs_only_with_select_os_shards_or_an_archive(self):
        select = GATE['jobs']['select']
        self.assertEqual(select['if'], "(inputs.select-os != '' && inputs.select-os != '[]') || inputs.test-shards != 0"
                                       " || inputs.archive-command != ''")
        self.assertEqual(GATE['on']['workflow_call']['inputs']['select-os']['default'], '[]')
        checkout = next(s for s in select['steps'] if 'actions/checkout' in s.get('uses', ''))
        self.assertEqual(checkout['with']['fetch-depth'], '2')


if __name__ == '__main__':
    unittest.main()

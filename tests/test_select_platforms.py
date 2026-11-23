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

READ = 'crates/leaf/tests/fixtures'
CRATES = {
    # name: (source, dependencies as (name, kind, optional), declared reads)
    'leaf': ('pub fn f() {}\n// An OOXML target, not a path: "../geom/media/image1.png"\n', [], []),
    'plat': ('#[cfg(windows)]\npub fn f() {}\n', [], []),
    'user': ('pub fn g() {}\n', [('plat', None, False)], []),
    'web': ('#[cfg(not(target_family = "wasm"))]\npub fn f() {}\n', [('leaf', None, False)], []),
    'geom': ('pub fn f() {}\n', [], []),
    'shape': ('pub fn f() {}\n', [('geom', None, False)], []),
    'checker': ('pub fn f() {}\n', [('leaf', 'dev', False)], []),
    'opt': ('#[cfg(feature = "leaf")]\npub use leaf::f;\n', [('leaf', None, True)], []),
    'reader': ('#[cfg(test)]\nconst X: &[u8] = include_bytes!("../../leaf/tests/fixtures/x.bin");\n'
               'const Y: &str = "../../../../etc/passwd";\n', [], [READ]),
    'old': ('pub fn f() {}\n', [], []),
}
EVERY_MEMBER = sorted(CRATES)


class SelectPlatformsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name, 'repo')
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'test')
        self.write('Cargo.toml', '[workspace]\nmembers = ["crates/*"]\n')
        self.write('Cargo.lock', 'version = 4\n')
        self.write(f'{READ}/x.bin', 'fixture\n')
        self.crates = {name: (deps, reads) for name, (_, deps, reads) in CRATES.items()}
        for name, (source, _, _) in CRATES.items():
            self.write(f'crates/{name}/Cargo.toml', f'[package]\nname = "{name}"\n')
            self.write(f'crates/{name}/src/lib.rs', source)
        bin_dir = Path(self.temp.name, 'bin')
        bin_dir.mkdir()
        self.write_metadata()
        cargo = bin_dir / 'cargo'
        cargo.write_text(f'#!/bin/sh\ncat "{self.temp.name}/metadata.json"\n')
        cargo.chmod(0o755)
        self.path = f'{bin_dir}:{os.environ["PATH"]}'
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'base')

    def write_metadata(self):
        packages = []
        for name, (deps, reads) in self.crates.items():
            packages.append({'name': name, 'id': f'path+file://{name}',
                             'manifest_path': str(self.repo / f'crates/{name}/Cargo.toml'),
                             'metadata': {'craft': {'reads': reads}} if reads else None,
                             'dependencies': [{'name': d, 'kind': kind, 'optional': optional,
                                               'path': str(self.repo / f'crates/{d}')}
                                              for d, kind, optional in deps]})
        metadata = {'packages': packages, 'workspace_members': [p['id'] for p in packages]}
        Path(self.temp.name, 'metadata.json').write_text(json.dumps(metadata))

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

    def invoke(self, event='pull_request', labels=(), crates='', paths='', docs='', select=SELECT,
               select_tests='true'):
        output = Path(self.temp.name, 'output')
        output.write_text('')
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--os', EVERY, '--select-os', select, '--label', 'full-ci',
             '--crates', crates, '--paths', paths, '--docs', docs, '--select-tests', select_tests],
            cwd=self.repo, capture_output=True, text=True,
            env={**os.environ, 'PATH': self.path, 'EVENT': event, 'LABELS': json.dumps(list(labels)),
                 'GITHUB_OUTPUT': str(output)})
        return result, output

    def run_select(self, **kwargs):
        result, output = self.invoke(**kwargs)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        fields = dict(line.split('=', 1) for line in output.read_text().splitlines())
        fields['os'] = json.loads(fields['os'])
        return fields

    def select(self, **kwargs):
        return self.run_select(select_tests='false', **kwargs)['os']

    def selection(self, **kwargs):
        """(tests, the selected members or 'all()'/'none()', skip) for the current change."""
        fields = self.run_select(**kwargs)
        selected = fields['filter']
        if selected not in ('all()', 'none()'):
            selected = [term.removeprefix('package(=').removesuffix(')') for term in selected.split(' | ')]
        return fields['tests'], selected, fields['skip'].split()

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


    # Test selection (--select-tests).

    def test_a_changed_leaf_crate_runs_its_tests_and_its_dependents(self):
        self.change('crates/leaf/src/lib.rs')
        tests, selected, skip = self.selection()
        # web depends on it, checker as a dev-dependency only, opt through an optional dependency.
        self.assertEqual((tests, selected), ('some', ['checker', 'leaf', 'opt', 'web']))
        self.assertEqual(skip, [m for m in EVERY_MEMBER if m not in selected])

    def test_a_changed_shared_crate_runs_every_crate_that_depends_on_it(self):
        self.change('crates/geom/src/lib.rs')
        self.assertEqual(self.selection()[:2], ('some', ['geom', 'shape']))

    def test_a_selection_of_every_member_is_all(self):
        for name in ('leaf', 'plat', 'geom', 'reader', 'old'):
            self.write(f'crates/{name}/src/lib.rs', (self.repo / f'crates/{name}/src/lib.rs').read_text() + '// changed\n')
        self.git('commit', '-q', '-am', 'change')
        self.assertEqual(self.selection(), ('all', 'all()', []))

    def test_a_fixture_selects_its_crate_and_the_crates_that_declare_reading_it(self):
        self.change(f'{READ}/x.bin')
        self.assertEqual(self.selection()[:2], ('some', ['checker', 'leaf', 'opt', 'reader', 'web']))
        self.change('crates/leaf/tests/other.rs')
        self.assertEqual(self.selection()[:2], ('some', ['checker', 'leaf', 'opt', 'web']))

    def test_a_member_manifest_runs_every_test_but_not_every_leg(self):
        self.change('crates/leaf/Cargo.toml')
        fields = self.run_select()
        self.assertEqual((fields['tests'], fields['filter'], fields['os']), ('all', 'all()', LINUX))
        self.assertIn('member manifest', fields['tests-reason'])

    def test_the_build_setup_and_files_outside_the_crates_run_every_test(self):
        for path in ('Cargo.lock', 'Cargo.toml', '.github/workflows/workspace.yml', '.config/nextest.toml',
                     'scripts/ci.sh', 'vendor/lib/src/lib.rs'):
            with self.subTest(path=path):
                self.change(path)
                self.assertEqual(self.selection(), ('all', 'all()', []))

    def test_documentation_only_runs_no_test(self):
        self.change('docs/guide.md')
        self.change('README.md')
        self.assertEqual(self.selection(), ('none', 'none()', EVERY_MEMBER))

    def test_extra_docs_globs_count_as_documentation_for_legs_and_tests(self):
        self.change('log/2026-11-20.txt')
        self.assertEqual(self.selection()[0], 'all')
        self.assertEqual(self.select(), ALL)
        self.assertEqual(self.selection(docs='log/*\nscorecard/*')[0], 'none')
        self.assertEqual(self.select(docs='log/*'), LINUX)

    def test_a_deleted_or_renamed_crate_runs_every_test(self):
        for rename in (False, True):
            with self.subTest(rename=rename):
                self.setUp()
                deps, reads = self.crates.pop('old')
                if rename:
                    self.git('mv', 'crates/old', 'crates/new')
                    self.crates['new'] = (deps, reads)
                else:
                    self.git('rm', '-q', '-r', 'crates/old')
                self.write_metadata()
                self.git('commit', '-q', '-m', 'move')
                fields = self.run_select()
                # A rename meets the new manifest first, a deletion the old crate's files.
                self.assertEqual(fields['tests'], 'all')
                self.assertIn('crates/new/Cargo.toml' if rename else 'crates/old/', fields['tests-reason'])

    def test_the_label_and_other_events_run_every_test(self):
        self.change('crates/leaf/src/lib.rs')
        self.assertEqual(self.selection(labels=['full-ci']), ('all', 'all()', []))
        for event in ('push', 'schedule', 'workflow_dispatch'):
            with self.subTest(event=event):
                self.assertEqual(self.selection(event=event), ('all', 'all()', []))

    def test_without_select_tests_every_test_runs(self):
        self.change('docs/guide.md')
        fields = self.run_select(select_tests='false')
        self.assertEqual((fields['tests'], fields['filter'], fields['skip']), ('all', 'all()', ''))

    def test_an_undeclared_or_stale_read_fails(self):
        self.change('crates/leaf/src/lib.rs')
        cases = {
            'undeclared include': ('reader', CRATES['reader'][0], []),
            'undeclared manifest join': (
                'geom', 'fn p() -> std::path::PathBuf {\n    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))\n'
                '        .join("../shape/data")\n}\n', []),
            'stale declaration': ('geom', 'pub fn f() {}\n', ['crates/leaf/tests/missing']),
        }
        for case, (name, source, reads) in cases.items():
            with self.subTest(case=case):
                self.setUp()
                self.write(f'crates/{name}/src/lib.rs', source)
                self.write('crates/shape/data/x.txt', 'x\n')
                self.crates[name] = (self.crates[name][0], reads)
                self.write_metadata()
                self.git('add', '-A')
                self.git('commit', '-q', '-m', 'read')
                result, _ = self.invoke()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('[package.metadata.craft] reads', result.stdout + result.stderr)
                # Without test selection the leg selection does not check reads.
                self.assertEqual(self.invoke(select_tests='false')[0].returncode, 0)


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
                                       " || inputs.archive-command != '' || inputs.shard-archives")
        self.assertEqual(GATE['on']['workflow_call']['inputs']['select-os']['default'], '[]')
        checkout = next(s for s in select['steps'] if 'actions/checkout' in s.get('uses', ''))
        self.assertEqual(checkout['with']['fetch-depth'], '2')


if __name__ == '__main__':
    unittest.main()

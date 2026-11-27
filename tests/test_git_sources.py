"""Run the release source gate with synthetic lockfiles and no Cargo network access."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / '.github/workflows/release.yml').read_text(), Loader=yaml.BaseLoader)
SCRIPT = next(step['run'] for step in DATA['jobs']['git-sources']['steps'] if 'run' in step)
REVISION = '033f7c3a67b825b8f222288788cc61234c0c945d'
EDIT_SESSION = {
    'name': 'edit-session', 'version': '0.1.0-alpha.2',
    'source': f'git+https://github.com/PerchAnton/edit-session?rev={REVISION}#{REVISION}',
}


class GitSourceTests(unittest.TestCase):
    """Exercise the actual shell/Python gate, including its fail-closed boundaries."""

    def run_gate(self, packages, repository='storytold/pdfcraft', configs=None,
                 metadata_status=0, lockfile=True):
        """Run the workflow step in isolation with a fake, argument-recording Cargo."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if lockfile:
                (root / 'Cargo.lock').write_text('version = 4\n' + ''.join(
                    '\n[[package]]\n' + ''.join(f'{k} = {json.dumps(v)}\n' for k, v in p.items())
                    for p in packages))
            for name, source in (configs or {}).items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
            commands = root / 'commands'
            commands.mkdir()
            cargo = commands / 'cargo'
            cargo.write_text(f'#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n'
                             'Path("cargo-args.json").write_text(json.dumps(sys.argv[1:]))\n'
                             f'sys.exit({metadata_status})\n')
            cargo.chmod(0o755)
            env = dict(os.environ, GITHUB_REPOSITORY=repository,
                       PATH=str(commands) + os.pathsep + os.environ['PATH'])
            result = subprocess.run(['bash', '-eu', '-c', SCRIPT], cwd=root, env=env,
                                    text=True, capture_output=True, timeout=10)
            arguments = root / 'cargo-args.json'
            return result, json.loads(arguments.read_text()) if arguments.exists() else None

    def test_pdfcraft_accepts_only_its_exact_edit_session_pin(self):
        """The proposed PdfCraft dependency can pass without relaxing other sources."""
        result, arguments = self.run_gate([EDIT_SESSION])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(arguments, ['metadata', '--locked', '--format-version', '1'])

    def test_exception_does_not_extend_to_other_repositories(self):
        """Forks, other apps and absent caller identity keep the strict default."""
        for repository in ('storytold/photocraft', 'PerchAnton/pdfcraft', '', 'other/pdfcraft'):
            with self.subTest(repository=repository):
                result, _ = self.run_gate([EDIT_SESSION], repository=repository)
                self.assertNotEqual(result.returncode, 0)

    def test_name_version_requested_revision_and_resolved_commit_must_all_match(self):
        """Changing either revision representation or package identity revokes the exception."""
        variants = [dict(EDIT_SESSION, name='another-package'),
                    dict(EDIT_SESSION, version='0.1.0-alpha.3')]
        for source in (
            EDIT_SESSION['source'].replace('?rev=' + REVISION, '?rev=main'),
            EDIT_SESSION['source'].replace('#' + REVISION, '#' + '0' * 40),
            EDIT_SESSION['source'].replace('/edit-session?', '/another-repo?'),
        ):
            variants.append(dict(EDIT_SESSION, source=source))
        for package in variants:
            with self.subTest(package=package):
                result, _ = self.run_gate([package])
                self.assertNotEqual(result.returncode, 0)

    def test_one_allowed_package_does_not_allow_a_second_external_source(self):
        """An exact pilot pin cannot conceal another foreign Git dependency."""
        foreign = dict(EDIT_SESSION, name='unreviewed', source='git+https://github.com/other/code#123')
        result, _ = self.run_gate([EDIT_SESSION, foreign])
        self.assertNotEqual(result.returncode, 0)

    def test_existing_storytold_registry_and_local_packages_still_pass(self):
        """The default source policy remains unchanged for every other caller."""
        packages = [
            {'name': 'local', 'version': '1.0.0'},
            {'name': 'registry', 'version': '1.0.0', 'source': 'registry+https://github.com/rust-lang/crates.io-index'},
            {'name': 'craft-ui', 'version': '1.0.0', 'source': 'git+https://github.com/storytold/craft-libs#123'},
        ]
        for repository in ('storytold/pdfcraft', 'storytold/photocraft', ''):
            with self.subTest(repository=repository):
                result, _ = self.run_gate(packages, repository=repository)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_source_replacement_patch_and_path_overrides_are_still_refused(self):
        """Both Cargo config spellings reject substitutions before invoking Cargo."""
        for filename in ('.cargo/config', '.cargo/config.toml'):
            for config in ('[source.crates-io]\nreplace-with="other"\n',
                           '[patch.crates-io]\n', 'paths=["../other"]\n'):
                with self.subTest(filename=filename, config=config):
                    result, arguments = self.run_gate([EDIT_SESSION], configs={filename: config})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIsNone(arguments)

    def test_missing_lockfile_and_metadata_failure_still_fail(self):
        """The exception cannot bypass lockfile presence or Cargo's locked resolution."""
        result, arguments = self.run_gate([], lockfile=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(arguments)
        result, _ = self.run_gate([EDIT_SESSION], metadata_status=23)
        self.assertEqual(result.returncode, 23)


if __name__ == '__main__':
    unittest.main()

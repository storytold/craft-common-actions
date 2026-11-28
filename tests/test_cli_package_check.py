"""The RFC-0018 package check: completions and one man page per subcommand in a Linux or FreeBSD package."""
from pathlib import Path
import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('check_cli_package', ROOT / 'actions/cli-package-check/check_cli_package.py')
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
ACTION = yaml.load((ROOT / 'actions/cli-package-check/action.yml').read_text(), Loader=yaml.BaseLoader)

FULL = [
    'usr/bin/app-cli',
    'usr/share/bash-completion/completions/app-cli',
    'usr/share/zsh/site-functions/_app-cli',
    'usr/share/fish/vendor_completions.d/app-cli.fish',
    'usr/share/man/man1/app-cli.1.gz',
    'usr/share/man/man1/app-cli-convert.1.gz',
    'usr/share/man/man1/app-cli-info.1.gz',
]


def run(argv, listing):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = CHECK.main(argv, stdin=io.StringIO('\n'.join(listing)))
    return code, out.getvalue(), err.getvalue()


class MissingFiles(unittest.TestCase):
    def test_a_complete_package_has_nothing_missing(self):
        self.assertEqual(CHECK.missing_files(FULL, 'app-cli', ['convert', 'info']), [])

    def test_each_missing_file_is_named(self):
        for dropped, text in [
            ('bash-completion', 'bash completion'),
            ('zsh/site-functions', 'zsh completion'),
            ('fish/vendor_completions.d', 'fish completion'),
            ('man1/app-cli.1', 'man page man/man1/app-cli.1'),
            ('man1/app-cli-info.1', 'man page man/man1/app-cli-info.1'),
        ]:
            with self.subTest(dropped=dropped):
                paths = [p for p in FULL if dropped not in p]
                missing = CHECK.missing_files(paths, 'app-cli', ['convert', 'info'])
                self.assertEqual(len(missing), 1)
                self.assertIn(text, missing[0])

    def test_a_subcommand_without_a_man_page_is_missing_only_when_listed(self):
        self.assertEqual(CHECK.missing_files(FULL, 'app-cli', []), [])
        self.assertEqual(len(CHECK.missing_files(FULL, 'app-cli', ['run'])), 1)

    def test_freebsd_paths_under_usr_local_and_other_compressions_match(self):
        paths = ['/usr/local/' + p.removeprefix('usr/').replace('.1.gz', '.1.zst') for p in FULL]
        self.assertEqual(CHECK.missing_files(paths, 'app-cli', ['convert', 'info']), [])

    def test_a_freebsd_tarball_keeps_man_pages_under_man_man1(self):
        paths = [p.replace('usr/share/man/', 'man/') for p in FULL]
        self.assertEqual(CHECK.missing_files(paths, 'app-cli', ['convert', 'info']), [])

    def test_another_clis_files_do_not_count(self):
        paths = [p.replace('app-cli', 'other-cli') for p in FULL]
        self.assertEqual(len(CHECK.missing_files(paths, 'app-cli', ['convert'])), 5)

    def test_a_cli_name_with_a_dot_is_not_a_pattern(self):
        paths = [p.replace('app-cli', 'appXcli') for p in FULL]
        self.assertEqual(len(CHECK.missing_files(paths, 'app.cli', [])), 4)

    def test_a_directory_entry_is_not_a_file(self):
        self.assertEqual(len(CHECK.missing_files(['usr/share/man/man1/app-cli.1/'], 'app-cli', [])), 4)


class Command(unittest.TestCase):
    def test_a_complete_list_passes(self):
        code, out, _ = run(['--cli', 'app-cli', '--subcommand', 'convert'], FULL)
        self.assertEqual(code, 0)
        self.assertIn('2 man pages', out)

    def test_a_missing_file_fails_and_is_named(self):
        code, _, err = run(['--cli', 'app-cli'], FULL[:3])
        self.assertEqual(code, 1)
        self.assertIn('fish completion', err)
        self.assertIn('man page man/man1/app-cli.1', err)

    def test_a_subcommand_without_a_man_page_fails(self):
        code, _, err = run(['--cli', 'app-cli', '--subcommand', 'info', '--subcommand', 'run'], FULL)
        self.assertEqual(code, 1)
        self.assertIn('app-cli-run.1', err)
        self.assertNotIn('app-cli-info.1', err)

    def test_an_empty_list_fails_rather_than_passing(self):
        code, _, err = run(['--cli', 'app-cli'], [])
        self.assertEqual(code, 1)
        self.assertIn('empty', err)


class ActionFile(unittest.TestCase):
    def test_inputs_reach_the_script_only_through_env(self):
        for step in ACTION['runs']['steps']:
            self.assertNotIn('${{', step['run'])

    def test_every_input_is_read(self):
        env = ACTION['runs']['steps'][0]['env']
        used = ' '.join(env.values())
        for name in ACTION['inputs']:
            self.assertIn(f'inputs.{name}', used)

    def test_the_script_opens_no_file_and_starts_no_process(self):
        source = (ROOT / 'actions/cli-package-check/check_cli_package.py').read_text()
        for word in ('subprocess', 'open(', 'os.system'):
            self.assertNotIn(word, source)


if __name__ == '__main__':
    unittest.main()

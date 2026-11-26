"""The RFC-0018 package check: completions and one man page per subcommand in a Linux or FreeBSD package."""
from pathlib import Path
import importlib.util
import io
import os
import stat
import tarfile
import tempfile
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
HELP = """Usage: app-cli <COMMAND>

Commands:
  convert  Convert a file
  info     Show details
  help     Print this message or the help of the given subcommand(s)

Options:
  -h, --help  Print help
"""


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = CHECK.main(argv)
    return code, out.getvalue(), err.getvalue()


def write(directory, name, text):
    path = Path(directory) / name
    path.write_text(text)
    return str(path)


class MissingFiles(unittest.TestCase):
    def test_a_complete_package_has_nothing_missing(self):
        self.assertEqual(CHECK.missing_files(FULL, 'app-cli', ['convert', 'info']), [])

    def test_each_missing_file_is_named(self):
        for dropped, text in [
            ('bash-completion', 'bash completion'),
            ('zsh/site-functions', 'zsh completion'),
            ('fish/vendor_completions.d', 'fish completion'),
            ('man1/app-cli.1', 'man page share/man/man1/app-cli.1'),
            ('man1/app-cli-info.1', 'man page share/man/man1/app-cli-info.1'),
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

    def test_another_clis_files_do_not_count(self):
        paths = [p.replace('app-cli', 'other-cli') for p in FULL]
        self.assertEqual(len(CHECK.missing_files(paths, 'app-cli', ['convert'])), 5)

    def test_a_cli_name_with_a_dot_is_not_a_pattern(self):
        paths = [p.replace('app-cli', 'appXcli') for p in FULL]
        self.assertEqual(len(CHECK.missing_files(paths, 'app.cli', [])), 4)

    def test_a_directory_entry_is_not_a_file(self):
        self.assertEqual(len(CHECK.missing_files(['usr/share/man/man1/app-cli.1/'], 'app-cli', [])), 4)


class Subcommands(unittest.TestCase):
    def test_help_block_names_the_commands_without_clap_help(self):
        self.assertEqual(CHECK.subcommands_from_help(HELP), ['convert', 'info'])

    def test_no_commands_block_gives_nothing(self):
        self.assertEqual(CHECK.subcommands_from_help('Usage: app-cli\n'), [])


class Command(unittest.TestCase):
    def test_a_listing_file_passes_and_fails(self):
        with tempfile.TemporaryDirectory() as d:
            good = write(d, 'good.txt', '\n'.join(FULL))
            bad = write(d, 'bad.txt', '\n'.join(FULL[:3]))
            code, out, _ = run(['--cli', 'app-cli', '--listing', good, '--subcommand', 'convert'])
            self.assertEqual(code, 0)
            self.assertIn('2 man pages', out)
            code, _, err = run(['--cli', 'app-cli', '--listing', bad])
            self.assertEqual(code, 1)
            self.assertIn('fish completion', err)

    def test_a_tar_archive_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            archive = os.path.join(d, 'app.tar.gz')
            with tarfile.open(archive, 'w:gz') as tar:
                for name in FULL:
                    info = tarfile.TarInfo(name)
                    tar.addfile(info, io.BytesIO(b''))
            code, _, _ = run(['--cli', 'app-cli', '--package', archive, '--subcommand', 'info'])
            self.assertEqual(code, 0)
            code, _, err = run(['--cli', 'app-cli', '--package', archive, '--subcommand', 'run'])
            self.assertEqual(code, 1)
            self.assertIn('app-cli-run.1', err)

    def test_the_binary_help_adds_subcommands(self):
        with tempfile.TemporaryDirectory() as d:
            binary = Path(d) / 'app-cli'
            binary.write_text(f"#!/bin/sh\ncat <<'EOF'\n{HELP}EOF\n")
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            listing = write(d, 'l.txt', '\n'.join(p for p in FULL if 'app-cli-info' not in p))
            code, _, err = run(['--cli', 'app-cli', '--listing', listing, '--binary', str(binary)])
            self.assertEqual(code, 1)
            self.assertIn('app-cli-info.1', err)
            self.assertNotIn('app-cli-convert.1', err)

    def test_a_binary_without_a_commands_block_fails(self):
        with tempfile.TemporaryDirectory() as d:
            binary = Path(d) / 'app-cli'
            binary.write_text('#!/bin/sh\necho "Usage: app-cli"\n')
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            listing = write(d, 'l.txt', '\n'.join(FULL))
            code, _, err = run(['--cli', 'app-cli', '--listing', listing, '--binary', str(binary)])
            self.assertEqual(code, 1)
            self.assertIn('Commands:', err)


class ActionFile(unittest.TestCase):
    def test_inputs_reach_the_script_only_through_env(self):
        for step in ACTION['runs']['steps']:
            self.assertNotIn('${{', step['run'])

    def test_every_input_is_read(self):
        env = ACTION['runs']['steps'][0]['env']
        used = ' '.join(env.values())
        for name in ACTION['inputs']:
            self.assertIn(f'inputs.{name}', used)


if __name__ == '__main__':
    unittest.main()

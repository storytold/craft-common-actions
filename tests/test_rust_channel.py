"""actions/rust-channel: a checkout's rust-toolchain.toml decides what a job installs."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'actions/rust-channel'))
import channel  # noqa: E402

FULL = '''# the release jobs install what cargo uses
[toolchain]
channel = "1.100.0"
components = [
  "clippy",   # the lints the gate runs
  "rustfmt",
]
targets = ["wasm32-unknown-unknown", 'aarch64-apple-darwin']
profile = "minimal"

[other]
channel = "ignored"
'''


class RustChannelTests(unittest.TestCase):
    def file(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / 'rust-toolchain.toml'
        if text is not None:
            path.write_text(text)
        return path

    def test_without_a_file_the_callers_values_are_used(self):
        self.assertEqual(channel.resolve(self.file(None), 'stable', 'a, b', ''), ('stable', 'a,b', ''))

    def test_the_files_channel_wins_and_targets_and_components_merge(self):
        path = self.file('[toolchain]\nchannel = "1.100.0"\ncomponents = ["clippy", "rustfmt"]\ntargets = ["wasm32-unknown-unknown"]\n')
        self.assertEqual(
            channel.resolve(path, 'stable', 'aarch64-apple-darwin, wasm32-unknown-unknown', 'clippy'),
            ('1.100.0', 'wasm32-unknown-unknown,aarch64-apple-darwin', 'clippy,rustfmt'),
        )

    def test_a_file_without_a_channel_keeps_the_callers_toolchain(self):
        self.assertEqual(channel.resolve(self.file('[toolchain]\nprofile = "minimal"\n'), 'stable', '', '')[0], 'stable')

    def test_the_fallback_reader_reads_a_file_spread_over_lines(self):
        # The ubuntu-22.04 runners run Python 3.10, which has no `tomllib`: the release jobs must
        # still install the file's toolchain.
        self.assertEqual(channel.parse_minimal(FULL)['channel'], '1.100.0')
        self.assertEqual(channel.parse_minimal(FULL)['components'], ['clippy', 'rustfmt'])
        self.assertEqual(channel.parse_minimal(FULL)['targets'], ['wasm32-unknown-unknown', 'aarch64-apple-darwin'])

    @unittest.skipIf(channel.tomllib is None, 'no tomllib to compare against')
    def test_the_fallback_reader_agrees_with_tomllib(self):
        expected = channel.tomllib.loads(FULL)['toolchain']
        for key in ('channel', 'components', 'targets', 'profile'):
            self.assertEqual(channel.parse_minimal(FULL)[key], expected[key], key)

    def test_without_tomllib_the_resolution_still_reads_the_file(self):
        path = self.file(FULL)
        with mock.patch.object(channel, 'tomllib', None):
            self.assertEqual(
                channel.resolve(path, 'stable', 'wasm32-unknown-unknown', ''),
                ('1.100.0', 'wasm32-unknown-unknown,aarch64-apple-darwin', 'clippy,rustfmt'),
            )

    def test_a_key_written_as_one_string_is_one_entry(self):
        path = self.file('[toolchain]\ntargets = "wasm32-unknown-unknown"\n')
        self.assertEqual(channel.resolve(path, 'stable', '', '')[1], 'wasm32-unknown-unknown')

    def test_the_table_name_may_be_spaced_or_quoted(self):
        for header in ('[ toolchain ]', '["toolchain"]', "['toolchain']"):
            self.assertEqual(channel.parse_minimal(f'{header}\nchannel = "1.100.0"\n')['channel'], '1.100.0', header)
        # A nested table and an array table are other tables.
        for header in ('[toolchain.x]', '[[toolchain]]'):
            self.assertEqual(channel.parse_minimal(f'{header}\nchannel = "1.100.0"\n'), {}, header)

    def test_a_quoted_key_is_read(self):
        self.assertEqual(channel.parse_minimal('[toolchain]\n"channel" = "1.100.0"\n')['channel'], '1.100.0')

    def test_a_multi_line_string_keeps_its_lines(self):
        # TOML writes a value over several lines like this; the reader must not store it empty.
        text = '[toolchain]\nchannel = """\nnightly-2024-01-01"""\n'
        self.assertEqual(channel.parse_minimal(text)['channel'], 'nightly-2024-01-01')
        if channel.tomllib is not None:
            self.assertEqual(channel.tomllib.loads(text)['toolchain']['channel'], 'nightly-2024-01-01')
        literal = "[toolchain]\nchannel = '''\nnightly-2024-01-01'''\n"
        self.assertEqual(channel.parse_minimal(literal)['channel'], 'nightly-2024-01-01')

    def test_a_hash_inside_a_multi_line_string_is_content(self):
        self.assertEqual(channel.parse_minimal('[toolchain]\nchannel = """\nnightly#1"""\n')['channel'], 'nightly#1')

    def test_a_multi_line_string_in_another_table_is_not_a_header(self):
        # The ignored table's string contains what looks like this table: it is content.
        text = '[toolchain]\nchannel = "stable"\n\n[other]\nnotes = \'\'\'\n[toolchain]\nchannel = "nightly"\n\'\'\'\n'
        self.assertEqual(channel.parse_minimal(text)['channel'], 'stable')

    def test_a_closing_delimiter_may_be_followed_by_a_comment(self):
        text = '[toolchain]\nchannel = """stable"""  # the release line\ntargets = ["wasm32-unknown-unknown"]\n'
        parsed = channel.parse_minimal(text)
        self.assertEqual(parsed['channel'], 'stable')
        self.assertEqual(parsed['targets'], ['wasm32-unknown-unknown'])

    def test_a_multi_line_string_that_never_closes_is_reported(self):
        with self.assertRaises(SystemExit):
            channel.parse_minimal('[toolchain]\nchannel = """stable\n')

    def test_an_unterminated_string_is_reported(self):
        # Half a value must not become a toolchain: `tomllib` rejects this file too.
        with self.assertRaises(SystemExit):
            channel.parse_minimal('[toolchain]\nchannel = "nightly\n')

    def test_a_quote_escaped_by_a_backslash_does_not_close_the_string(self):
        with self.assertRaises(SystemExit):
            channel.parse_minimal('[toolchain]\nchannel = "nightly\\"\n')
        # A literal string has no escapes, so the same shape closes it.
        self.assertEqual(channel.parse_minimal("[toolchain]\nchannel = 'nightly\\'\n")['channel'], 'nightly\\')

    def test_an_array_element_that_never_closes_is_reported(self):
        with self.assertRaises(SystemExit):
            channel.parse_minimal('[toolchain]\ntargets = ["wasm32-unknown-unknown", "aarch64-apple-darwin]\n')

    def test_an_array_may_end_with_a_comma(self):
        self.assertEqual(
            channel.parse_minimal('[toolchain]\ntargets = ["wasm32-unknown-unknown", "aarch64-apple-darwin",]\n')['targets'],
            ['wasm32-unknown-unknown', 'aarch64-apple-darwin'],
        )

    def test_an_array_element_may_hold_a_comma(self):
        self.assertEqual(channel.parse_minimal('[toolchain]\ntargets = ["a,b", "c"]\n')['targets'], ['a,b', 'c'])

    def test_an_unterminated_array_is_reported(self):
        with self.assertRaises(SystemExit):
            channel.parse_minimal('[toolchain]\ntargets = ["wasm32-unknown-unknown"\n')


if __name__ == '__main__':
    unittest.main()

"""actions/rust-channel: a checkout's rust-toolchain.toml decides what a job installs."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'actions/rust-channel'))
import channel  # noqa: E402


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


if __name__ == '__main__':
    unittest.main()

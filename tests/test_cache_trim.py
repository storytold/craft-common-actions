"""actions/cache-trim: a saved entry keeps only the build units the run's build used."""
from pathlib import Path
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'actions/cache-trim'))
import trim  # noqa: E402


def unit(target, package, meta, profile='debug'):
    directory = target / profile / 'build' / package / meta
    (directory / 'out').mkdir(parents=True)
    (directory / 'fingerprint').mkdir()
    (directory / 'out' / 'artifact').write_bytes(b'x' * 100)
    return directory


class TrimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / 'target'
        self.record = self.root / 'units.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *messages, noise=()):
        lines = [json.dumps(m) for m in messages] + list(noise)
        self.record.write_text('\n'.join(lines) + '\n')

    def run_trim(self, record=None):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            removed = trim.trim(self.target, str(self.record if record is None else record))
        return removed, out.getvalue()

    def test_keeps_used_units_and_removes_older_builds_of_the_same_package(self):
        lib = unit(self.target, 'serde', '1111111111111111')
        stale = unit(self.target, 'serde', '2222222222222222')
        fresh_test = unit(self.target, 'photocraft-engine', '3333333333333333')
        script_run = unit(self.target, 'ring', '4444444444444444')
        gone = unit(self.target, 'old-dep', '5555555555555555')
        self.write(
            {'reason': 'compiler-artifact', 'fresh': True, 'filenames': [str(lib / 'out' / 'libserde.rlib')]},
            {'reason': 'compiler-artifact', 'fresh': False, 'filenames': [], 'executable': str(fresh_test / 'out' / 'engine-3333')},
            {'reason': 'build-script-executed', 'out_dir': str(script_run / 'out')},
            {'reason': 'build-finished', 'success': True},
            noise=['test foo ... ok', '{not json'])
        removed, out = self.run_trim()
        self.assertEqual(removed, 2)
        self.assertTrue(lib.is_dir() and fresh_test.is_dir() and script_run.is_dir())
        self.assertFalse(stale.exists())
        self.assertFalse(gone.parent.exists())
        self.assertIn('kept 3 units', out)
        self.assertIn('removed old-dep', out)

    def test_a_unit_reported_at_its_uplifted_copy_is_kept(self):
        # Cargo reports a unit asked for by name (a doctest's library, `cargo build -p` binaries) at
        # its hard-linked copy in the profile directory, not in the unit's out/.
        binary = unit(self.target, 'xtask', '1111111111111111')
        stale = unit(self.target, 'xtask', '2222222222222222')
        (binary / 'out' / 'xtask-1111111111111111').write_text('bin')
        (self.target / 'debug' / 'xtask').hardlink_to(binary / 'out' / 'xtask-1111111111111111')
        (self.target / 'debug' / 'copy').write_text('bin')  # same content, another file
        self.write({'reason': 'compiler-artifact', 'filenames': [str(self.target / 'debug' / 'xtask')],
                    'executable': str(self.target / 'debug' / 'xtask')},
                   {'reason': 'compiler-artifact', 'filenames': [str(self.target / 'debug' / 'copy')]})
        removed, out = self.run_trim()
        self.assertEqual(removed, 1)
        self.assertTrue(binary.is_dir())
        self.assertFalse(stale.exists())
        self.assertIn('xtask/2222222222222222', out)

    def test_target_triple_profiles_are_trimmed_too(self):
        used = unit(self.target, 'a', '1111111111111111')
        other = unit(self.target, 'b', '2222222222222222', profile='wasm32-unknown-unknown/debug')
        self.write({'reason': 'compiler-artifact', 'filenames': [str(used / 'out' / 'liba.rlib')]})
        self.run_trim()
        self.assertFalse(other.exists())

    def test_the_old_layout_is_left_to_rust_cache(self):
        used = unit(self.target, 'a', '1111111111111111')
        old = self.target / 'debug' / 'build' / 'a-1111111111111111'
        (old / 'out').mkdir(parents=True)
        deps = self.target / 'debug' / 'deps'
        deps.mkdir()
        (deps / 'liba-1111111111111111.rlib').write_text('x')
        self.write({'reason': 'compiler-artifact', 'filenames': [str(used / 'out' / 'liba.rlib')]})
        self.run_trim()
        self.assertTrue((old / 'out').is_dir())
        self.assertTrue((deps / 'liba-1111111111111111.rlib').is_file())

    def test_without_a_usable_record_nothing_is_deleted(self):
        kept = unit(self.target, 'a', '1111111111111111')
        self.assertEqual(self.run_trim(record='')[0], 0)
        self.assertEqual(self.run_trim(record=self.root / 'missing.jsonl')[0], 0)
        self.record.write_text('')
        self.assertEqual(self.run_trim()[0], 0)
        # A record of another target directory (a moved CARGO_TARGET_DIR) must not empty this one.
        self.write({'reason': 'compiler-artifact', 'filenames': ['/elsewhere/debug/build/a/1111111111111111/out/liba.rlib']})
        removed, out = self.run_trim()
        self.assertEqual(removed, 0)
        self.assertIn('::warning::', out)
        self.assertTrue(kept.is_dir())

    def test_a_unit_built_in_this_job_but_not_recorded_stops_the_trim(self):
        # A build command that does not record (a new step's `cargo build`) would otherwise lose its
        # units before every save and rebuild them every run, unnoticed.
        used = unit(self.target, 'a', '1111111111111111')
        stale = unit(self.target, 'a', '2222222222222222')
        unrecorded = unit(self.target, 'b', '3333333333333333')
        for path in (used, stale):
            os.utime(path / 'out' / 'artifact', (1_000_000, 1_000_000))  # restored: archived mtimes
        self.record.with_suffix('.start').write_text('2000000\n')
        self.write({'reason': 'compiler-artifact', 'filenames': [str(used / 'out' / 'liba.rlib')]})
        removed, out = self.run_trim()
        self.assertEqual(removed, 0)
        self.assertTrue(stale.is_dir() and unrecorded.is_dir())
        self.assertIn('::warning::cache-trim: 1 units built in this job are not in the record (b/3333333333333333)', out)
        # Built before the job started: only stale units are left, and they go.
        os.utime(unrecorded / 'out' / 'artifact', (1_000_000, 1_000_000))
        removed, _ = self.run_trim()
        self.assertEqual(removed, 2)
        self.assertTrue(used.is_dir())

    def test_only_what_was_deleted_counts_as_removed(self):
        used = unit(self.target, 'a', '1111111111111111')
        locked = unit(self.target, 'a', '2222222222222222')
        self.write({'reason': 'compiler-artifact', 'filenames': [str(used / 'out' / 'liba.rlib')]})
        (locked / 'out').chmod(0o500)  # its files cannot be unlinked
        try:
            removed, out = self.run_trim()
        finally:
            (locked / 'out').chmod(0o700)
        self.assertEqual(removed, 0)
        self.assertIn('removed 0 units (0.00 GB)', out)
        self.assertIn('::warning::cache-trim: 1 units could not be deleted completely: a/2222222222222222', out)

    def test_a_failure_is_a_warning(self):
        # gate.yml runs the step with continue-on-error: whatever was deleted was unused.
        def broken(target, record):
            raise PermissionError('denied')
        original, trim.trim = trim.trim, broken
        self.addCleanup(setattr, trim, 'trim', original)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(trim.main(), 1)
        self.assertIn("::warning::cache-trim stopped: PermissionError('denied')", out.getvalue())


if __name__ == '__main__':
    unittest.main()

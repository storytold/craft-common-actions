"""actions/cache-prune: one live entry per family, deleted as soon as its successor is saved."""
from pathlib import Path
import contextlib
import io
import re
import sys
import unittest
import unittest.mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'actions/cache-prune'))
import prune  # noqa: E402

PROFILE = '7' * 64
OTHER_PROFILE = '8' * 64


def entry(id_, key, created, accessed=None, size=10**9):
    return {'id': id_, 'key': key, 'created_at': f'2026-11-19T{created}:00Z',
            'last_accessed_at': f'2026-11-19T{accessed or created}:00Z', 'size_in_bytes': size}


def crates(run, attempt='1', profile=PROFILE):
    return f'v2-rust-{profile}-gate-crates-Linux-x64-{run}-{attempt}-Linux-x64-d843ffbc-3e8a8849'


class FamilyTests(unittest.TestCase):
    def test_families(self):
        self.assertEqual(prune.family(crates('44600646541'), []), ('rolling', 'v2-rust-gate-crates-Linux-x64'))
        self.assertEqual(prune.family(crates('812', '0'), []), ('rolling', 'v2-rust-gate-crates-Linux-x64'))
        self.assertEqual(prune.family(f'v2-rust-{PROFILE}-gate-Windows_NT-x64-ae62fac7-79024277', []),
                         ('replaced', 'v2-rust-gate-Windows_NT-x64'))
        self.assertEqual(prune.family('v1-rust-ci-web-web-Linux-x64-304bc087-3e8a8849', []),
                         ('replaced', 'v1-rust-ci-web-web-Linux-x64'))
        self.assertEqual(prune.family('corpus-' + 'c' * 64, ['corpus']), ('replaced', 'corpus'))

    def test_other_entries_are_never_touched(self):
        for key in ('corpus-' + 'c' * 64,  # content keys only when named
                    'apt-debs-v1-ubuntu24-' + '2' * 64 + '-20261116.376.1',
                    'harden-runner-cacheKey',
                    'determinatesystem-nix-installer-x86_64-linux-88d8c6a2d0f6cbacb0d4301ab84ecba4',
                    'sccache-' + 'a' * 64):
            with self.subTest(key=key):
                self.assertIsNone(prune.family(key, []))


class PlanTests(unittest.TestCase):
    def deleted(self, entries, names=()):
        return sorted(e['id'] for e, _ in prune.plan(entries, list(names)))

    def test_only_the_newest_rolling_entry_stays(self):
        entries = [entry(1, crates('100'), '10:00'), entry(2, crates('100', '0'), '11:00', '11:30'),
                   entry(3, crates('101', '0'), '12:00'), entry(4, crates('99', profile=OTHER_PROFILE), '09:00')]
        self.assertEqual(self.deleted(entries), [1, 2, 4])

    def test_a_replaced_entry_goes_once_nothing_used_it_since(self):
        old_web = entry(1, 'v1-rust-ci-web-web-Linux-x64-304bc087-aaaaaaaa', '10:00', '10:05')
        new_web = entry(2, 'v1-rust-ci-web-web-Linux-x64-304bc087-bbbbbbbb', '11:00')
        old_corpus = entry(3, 'corpus-' + 'a' * 64, '09:00', '11:10')  # still restored after the new one
        new_corpus = entry(4, 'corpus-' + 'b' * 64, '11:00')
        self.assertEqual(self.deleted([old_web, new_web, old_corpus, new_corpus], ['corpus']), [1])

    def test_os_and_job_are_separate_families(self):
        entries = [entry(1, f'v2-rust-{PROFILE}-gate-Windows_NT-x64-ae62fac7-79024277', '10:00'),
                   entry(2, 'v1-rust-ci-web-web-Linux-x64-304bc087-3e8a8849', '11:00'),
                   entry(3, crates('100').replace('Linux-x64', 'Windows_NT-x64'), '09:00'),
                   entry(4, crates('101'), '12:00')]
        self.assertEqual(self.deleted(entries), [])

    def test_a_rolling_entry_replaces_the_plain_entry_it_follows(self):
        # storytold/vectorcraft's main entries on 2026-11-27, and the first rolling gate entry.
        profile = 'a013abbb0b3813be455eef85113d3b9952597819a33feb6061a5f87a5cd184fb'

        def real(id_, key, created, accessed):
            return {'id': id_, 'key': key, 'created_at': created, 'last_accessed_at': accessed, 'size_in_bytes': 10**9}
        entries = [
            real(1, f'v2-rust-{profile}-gate-Linux-x64-46055934-01d60c35',
                 '2026-11-27T05:58:09.801683Z', '2026-11-27T19:14:51.898171Z'),
            real(2, f'v1-affinity-rust-{profile}-gate-Linux-x64-b541f517-01d60c35',
                 '2026-11-27T05:02:11.656569Z', '2026-11-27T23:35:30.238353Z'),
            real(3, 'v0-rust-build-Linux-x64-1fd829d9-01d60c35', '2026-11-27T23:20:00Z', '2026-11-27T23:20:00Z'),
            real(4, f'v2-rust-{profile}-gate-crates-Linux-x64-0-0-Linux-x64-46055934-01d60c35',
                 '2026-11-28T01:00:00Z', '2026-11-28T01:00:00Z'),
        ]
        self.assertEqual(self.deleted(entries), [1])

    def test_only_an_unused_plain_entry_of_the_same_job_goes(self):
        entries = [
            entry(1, f'v2-rust-{PROFILE}-gate-Linux-x64-304bc087-3e8a8849', '09:00', '12:30'),  # used since
            entry(2, crates('100'), '12:00'),
            entry(3, f'v2-rust-{PROFILE}-web-Linux-x64-304bc087-3e8a8849', '09:00', '10:00'),
            entry(4, crates('100').replace('-gate-', '-web-'), '11:00'),
            entry(5, f'v2-rust-{PROFILE}-gate-Windows_NT-x64-ae62fac7-79024277', '09:00'),  # no Windows rolling entry
            entry(6, crates('100').replace('-gate-', '-docs-'), '10:00'),
            entry(7, f'v2-rust-{PROFILE}-docs-Linux-x64-304bc087-3e8a8849', '11:00'),  # newer than the rolling one
        ]
        self.assertEqual(self.deleted(entries), [3])

    def test_a_plain_entry_on_another_architecture_stays(self):
        entries = [entry(1, f'v2-rust-{PROFILE}-gate-Linux-arm64-304bc087-3e8a8849', '09:00'),
                   entry(2, crates('100'), '12:00')]
        self.assertEqual(self.deleted(entries), [])

    def test_a_plain_entry_of_another_job_stays(self):
        entries = [entry(1, f'v2-rust-{PROFILE}-lint-Linux-x64-304bc087-3e8a8849', '09:00'),
                   entry(2, crates('100'), '12:00')]
        self.assertEqual(self.deleted(entries), [])


class RefTests(unittest.TestCase):
    def test_the_cache_list_asks_for_one_ref(self):
        api = prune.Api('o/r', 't', url='https://api.example')
        paths = []
        with unittest.mock.patch.object(api, 'pages', side_effect=lambda path, field: paths.append(path) or []):
            api.caches('refs/heads/main')
        self.assertIn('ref=refs%2Fheads%2Fmain', paths[0])

    def test_entries_on_another_ref_are_untouched(self):
        main, pull = 'refs/heads/main', 'refs/pull/7/merge'

        class RefApi:
            def __init__(self, entries):
                self.entries = entries
                self.deleted = []

            def caches(self, ref):  # without a ref the API lists every ref's entries
                return [e for e, r in self.entries if ref is None or r == ref]

            def delete(self, cache_id):
                self.deleted.append(cache_id)
        api = RefApi([(entry(1, f'v2-rust-{PROFILE}-gate-Linux-x64-304bc087-3e8a8849', '09:00'), main),
                      (entry(2, crates('100'), '12:00'), main),
                      (entry(3, f'v2-rust-{PROFILE}-gate-Linux-x64-304bc087-3e8a8849', '09:00'), pull),
                      (entry(4, crates('99'), '08:00'), pull)])
        with contextlib.redirect_stdout(io.StringIO()):
            prune.prune(api, main, [], False)
        self.assertEqual(api.deleted, [1])


class FakeApi:
    def __init__(self, entries, job_polls, late=(), failures=0):
        self.entries = list(entries)
        self.job_polls = list(job_polls)
        self.late = list(late)  # (listing from which it shows, entry): a cache list that lags
        self.failures = failures
        self.deleted = []
        self.listed = 0

    def caches(self, ref):
        self.listed += 1
        if self.failures:
            self.failures -= 1
            raise prune.urllib.error.HTTPError('u', 502, 'Bad Gateway', None, None)
        shown = self.entries + [e for n, e in self.late if self.listed >= n]
        return [e for e in shown if e['id'] not in self.deleted]

    def delete(self, cache_id):
        self.deleted.append(cache_id)

    def jobs(self, run, attempt):
        return self.job_polls.pop(0)


def job(id_, name, status, runner='r', started=None, conclusion=None):
    found = {'id': id_, 'name': name, 'status': status, 'runner_name': runner}
    if started:
        found.update(started_at=f'2026-11-19T{started}:00Z', conclusion=conclusion or 'success')
    return found


SAVING = [re.compile(p) for p in (r'^gate( / build \(.*\))?$', r'^windows( / build \(.*\))?$')]


class WatchTests(unittest.TestCase):
    def watch(self, api, patterns=SAVING, dry_run=False, minutes=110, clock=None):
        self.sleeps = 0

        def sleep(_):
            self.sleeps += 1
        with contextlib.redirect_stdout(io.StringIO()) as out:
            prune.watch(api, 'refs/heads/main', [], dry_run, '1', '1', patterns, 'me', minutes,
                        sleep=sleep, clock=clock or (lambda: 0))
        return out.getvalue()

    def test_prunes_when_a_saving_job_finishes_and_ends_when_none_is_left(self):
        me = job(0, 'cache prune', 'in_progress', runner='me')
        polls = [
            [me, job(1, 'select platforms', 'in_progress')],
            [me, job(1, 'select platforms', 'completed'), job(2, 'gate / build (ubuntu-24.04)', 'in_progress')],
            [me, job(2, 'gate / build (ubuntu-24.04)', 'completed'), job(3, 'windows', 'completed'),
             job(4, 'gate / test 1/4 (ubuntu-24.04)', 'in_progress')],
        ]
        api = FakeApi([entry(1, crates('100'), '10:00'), entry(2, crates('100', '0'), '11:00', size=4 * 10**9)], polls)
        out = self.watch(api)
        self.assertEqual(api.deleted, [1])
        self.assertEqual(api.job_polls, [])  # it ended with a shard still running
        self.assertEqual(api.listed, 2)  # the first pass, and the one after the build
        self.assertIn(f"delete {crates('100')} (1.00 GB)", out)

    def test_a_saving_job_not_listed_yet_keeps_it_going(self):
        polls = [[job(1, 'gate / build (ubuntu-24.04)', 'completed')],
                 [job(1, 'gate / build (ubuntu-24.04)', 'completed'), job(2, 'windows / build (windows-latest)', 'in_progress')],
                 [job(1, 'gate / build (ubuntu-24.04)', 'completed'), job(2, 'windows / build (windows-latest)', 'completed')]]
        api = FakeApi([], polls)
        self.watch(api)
        self.assertEqual(api.job_polls, [])

    def test_without_patterns_it_ends_after_two_idle_polls(self):
        polls = [[job(1, 'a', 'in_progress')], [job(1, 'a', 'completed')], [job(1, 'a', 'completed')], [job(9, 'never read', 'queued')]]
        api = FakeApi([], polls)
        self.watch(api, patterns=[])
        self.assertEqual(len(api.job_polls), 1)

    def test_the_last_pass_waits_for_the_last_saving_jobs_entry(self):
        # The cache list shows the Windows entry two listings after the job completed: the old one
        # must still go in this run.
        polls = [[job(1, 'gate / build (x)', 'completed', started='10:00'),
                  job(2, 'windows / build (w)', 'completed', started='10:30')]]
        old = entry(1, f'v2-rust-{PROFILE}-gate-Windows_NT-x64-ae62fac7-79024277', '09:00')
        new = entry(2, f'v2-rust-{PROFILE}-gate-Windows_NT-x64-ae62fac7-12345678', '11:00')
        api = FakeApi([old], polls, late=[(4, new)])
        self.watch(api)
        self.assertEqual(api.deleted, [1])
        self.assertEqual(api.listed, 4)

    def test_the_last_pass_gives_up_when_no_entry_appears(self):
        # A job whose key already existed saves nothing.
        polls = [[job(1, 'gate / build (x)', 'completed', started='10:00'), job(2, 'windows', 'completed', started='10:00')]]
        api = FakeApi([entry(1, crates('100'), '09:00')], polls)
        self.watch(api)
        self.assertEqual(api.listed, 2 + prune.FINAL_POLLS)

    def test_a_skipped_saving_job_is_not_waited_for(self):
        polls = [[job(1, 'gate / build (x)', 'completed', started='10:00', conclusion='skipped'),
                  job(2, 'windows', 'completed', started='10:00', conclusion='skipped')]]
        api = FakeApi([], polls)
        self.watch(api)
        self.assertEqual(api.listed, 2)

    def test_a_failed_pass_warns_and_the_next_one_prunes(self):
        polls = [[job(1, 'gate / build (x)', 'in_progress')],
                 [job(1, 'gate / build (x)', 'completed'), job(2, 'windows', 'completed')]]
        api = FakeApi([entry(1, crates('100'), '10:00'), entry(2, crates('101'), '11:00')], polls, failures=1)
        out = self.watch(api)
        self.assertIn('::warning::cache-prune: a pass failed', out)
        self.assertEqual(api.deleted, [1])

    def test_it_ends_with_a_pass_at_the_deadline(self):
        ticks = iter(range(0, 10**6, 600))  # ten minutes per poll
        polls = [[job(1, 'gate / build (x)', 'in_progress')]] * 3
        api = FakeApi([], polls)
        out = self.watch(api, minutes=25, clock=lambda: next(ticks))
        self.assertIn('still watching after 25 minutes', out)
        self.assertEqual(api.listed, 2)

    def test_a_failure_that_stops_the_script_is_a_warning(self):
        env = {'PRUNE_REPO': 'o/r', 'GH_TOKEN': 't', 'PRUNE_REF': 'refs/heads/main'}

        def broken(*_):
            raise RuntimeError('boom')
        original, prune.prune = prune.prune, broken
        self.addCleanup(setattr, prune, 'prune', original)
        with unittest.mock.patch.dict(prune.os.environ, env), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(prune.main(), 1)
        self.assertIn("::warning::cache-prune stopped: RuntimeError('boom')", out.getvalue())

    def test_dry_run_deletes_nothing(self):
        api = FakeApi([entry(1, crates('100'), '10:00'), entry(2, crates('101'), '11:00')],
                      [[job(1, 'gate / build (x)', 'completed'), job(2, 'windows', 'completed')]])
        out = self.watch(api, dry_run=True)
        self.assertEqual(api.deleted, [])
        self.assertIn('would delete', out)


class DefinitionTests(unittest.TestCase):
    def test_the_action_runs_the_script_with_its_inputs(self):
        action = yaml.load((ROOT / 'actions/cache-prune/action.yml').read_text(), Loader=yaml.BaseLoader)
        env = action['runs']['steps'][0]['env']
        self.assertEqual(env['GH_TOKEN'], '${{ inputs.token }}')
        self.assertEqual(action['inputs']['watch']['default'], 'false')
        self.assertEqual(action['inputs']['dry-run']['default'], 'false')
        self.assertNotIn('${{', action['runs']['steps'][0]['run'])


if __name__ == '__main__':
    unittest.main()

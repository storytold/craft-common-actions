"""actions/cache-prune: one live entry per family, deleted as soon as its successor is saved."""
from pathlib import Path
import contextlib
import io
import re
import sys
import unittest
import unittest.mock
import urllib.parse

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


WARM = 'gate / cache warm (ubuntu-24.04)'
FAMILY = 'v2-rust-gate-crates-Linux-x64'


def at(hhmm):
    return prune.when(f'2026-11-19T{hhmm}:00Z')


def step(name, status, conclusion=None):
    return {'name': name, 'status': status, 'conclusion': conclusion}


def warm(status, *steps, conclusion=None, started='10:00', attempt=1):
    """The saving job of run 1 as the jobs API lists it, with its steps so far."""
    found = {'id': 5, 'name': WARM, 'status': status, 'runner_name': 'r', 'conclusion': conclusion, 'run_attempt': attempt,
             'started_at': f'2026-11-19T{started}:00Z', 'steps': [step('Setup', 'completed', 'success'), *steps]}
    return found


SAVING_NOW = warm('in_progress', step('Post Setup', 'in_progress'))
SAVED = crates('1')  # the key a cold build of run 1, attempt 1 saves


class ClearApi(FakeApi):
    def __init__(self, entries, job_polls, late=(), earlier=None):
        super().__init__(entries, job_polls, late)
        self.earlier = earlier or {}

    def jobs(self, run, attempt):
        if int(attempt) in self.earlier:
            return self.earlier[int(attempt)]
        return self.job_polls.pop(0)

    def run(self, run):
        return {'run_started_at': '2026-11-19T09:30:00Z'}


class ClearRollingTests(unittest.TestCase):
    """A cold run deletes the old rolling entries when its saving job starts saving the new one."""

    def clear(self, api, attempt='1', minutes=110, now=None, dry_run=False):
        self.sleeps = 0

        def sleep(_):
            self.sleeps += 1
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status = prune.clear_rolling(api, 'refs/heads/main', FAMILY, '1', attempt, re.compile(r'^gate / cache warm \('),
                                         'Post Setup', dry_run, 'me', minutes, sleep=sleep, now=now or (lambda: at('10:05')))
        return status, out.getvalue()

    def entries(self):
        return [entry(1, crates('100'), '09:00'),  # the entry the cold run replaces
                entry(2, crates('101'), '09:45'),  # saved by a push run after this run started
                entry(3, crates('100').replace('-gate-', '-web-'), '08:00'),  # another job's family
                entry(4, f'v2-rust-{PROFILE}-gate-Linux-x64-304bc087-3e8a8849', '08:00')]  # not rolling

    def test_the_old_entries_go_when_the_save_starts_and_a_successful_save_passes(self):
        polls = [[warm('in_progress', step('Run the warm build', 'in_progress'))],
                 [SAVING_NOW],
                 [warm('in_progress', step('Post Setup', 'completed', 'success'))]]
        api = ClearApi(self.entries(), polls, late=[(2, entry(9, SAVED, '10:40'))])
        status, out = self.clear(api)
        self.assertEqual(status, 0)
        self.assertEqual(api.deleted, [1])
        self.assertEqual(api.job_polls, [])
        self.assertIn(f"saved {SAVED}", out)

    def test_a_save_that_is_canceled_or_fails_after_the_deletion_fails_the_step(self):
        endings = {
            'canceled': warm('completed', step('Post Setup', 'completed', 'cancelled'), conclusion='cancelled'),
            'stopped mid-save': warm('completed', step('Post Setup', 'in_progress'), conclusion='cancelled'),
            'failed': warm('completed', step('Post Setup', 'completed', 'failure'), conclusion='failure'),
        }
        for name, ending in endings.items():
            with self.subTest(name):
                api = ClearApi(self.entries(), [[SAVING_NOW], [ending]])
                status, out = self.clear(api)
                self.assertEqual(status, 1)
                self.assertEqual(api.deleted, [1])
                self.assertIn('the next run starts cold', out)

    def test_a_save_that_already_failed_deletes_nothing(self):
        # The save step started and failed between two polls.
        ending = warm('completed', step('Post Setup', 'completed', 'failure'), conclusion='failure')
        api = ClearApi(self.entries(), [[warm('in_progress', step('Run the warm build', 'in_progress'))], [ending]])
        status, out = self.clear(api)
        self.assertEqual((status, api.deleted), (1, []))
        self.assertIn('nothing deleted', out)

    def test_a_pattern_that_matches_two_legs_deletes_nothing(self):
        windows = dict(SAVING_NOW, id=6, name='gate / cache warm (windows-latest)')
        api = ClearApi(self.entries(), [[windows, SAVING_NOW]])
        status, out = self.clear(api)
        self.assertEqual((status, api.deleted), (1, []))
        self.assertIn('matches 2 jobs', out)

    def test_a_failed_build_step_deletes_nothing(self):
        # The post step still runs the other post actions, but rust-cache saves nothing.
        failed = warm('in_progress', step('Run the warm build', 'completed', 'failure'), step('Post Setup', 'in_progress'))
        api = ClearApi(self.entries(), [[failed]])
        status, out = self.clear(api)
        self.assertEqual((status, api.deleted), (1, []))
        self.assertIn("'Run the warm build' failed", out)
        tolerated = warm('in_progress', step('Trim the cache entry to this build', 'completed', 'failure'),
                         step('Post Setup', 'completed', 'success'))
        api = ClearApi(self.entries(), [[tolerated]], late=[(1, entry(9, SAVED, '10:40'))])
        self.assertEqual((self.clear(api)[0], api.deleted), (0, [1]))

    def test_a_failed_post_step_before_the_save_deletes_nothing(self):
        failed = warm('in_progress', step('Post Upload logs', 'completed', 'failure'), step('Post Setup', 'in_progress'))
        api = ClearApi(self.entries(), [[failed]])
        status, out = self.clear(api)
        self.assertEqual((status, api.deleted), (1, []))
        self.assertIn("'Post Upload logs' failed", out)
        # A post step that runs after the save does not decide it.
        after = warm('in_progress', step('Post Setup', 'completed', 'success'), step('Post Checkout', 'completed', 'failure'))
        api = ClearApi(self.entries(), [[after]], late=[(1, entry(9, SAVED, '10:40'))])
        self.assertEqual((self.clear(api)[0], api.deleted), (0, [1]))

    def test_a_save_first_seen_after_the_deadline_deletes_nothing(self):
        # The leg started at 10:00 with 60 minutes; this poll first sees its save at 11:01.
        api = ClearApi(self.entries(), [[SAVING_NOW]])
        status, out = self.clear(api, minutes=60, now=lambda: at('11:01'))
        self.assertEqual((status, api.deleted), (1, []))
        self.assertIn('nothing deleted', out)

    def test_a_successful_step_that_saved_no_entry_fails(self):
        # Another run's entry saved after the leg started does not count as the leg's.
        polls = [[warm('in_progress', step('Post Setup', 'completed', 'success'))]]
        api = ClearApi(self.entries(), polls, late=[(1, entry(9, crates('300'), '10:40'))])
        status, out = self.clear(api)
        self.assertEqual(status, 1)
        self.assertEqual(api.listed, 2 + prune.FINAL_POLLS)
        self.assertIn('shows no v2-rust-gate-crates-Linux-x64 entry', out)

    def test_the_deadline_counts_from_the_saving_jobs_start(self):
        # It starts at 09:00 and the leg at 10:00: at 10:30 an hour has passed since the start,
        # but not since the leg started.
        times = iter([at('09:00'), at('10:30'), at('11:01')])
        polls = [[warm('in_progress', step('Run the warm build', 'in_progress'))]] * 2
        api = ClearApi(self.entries(), polls)
        status, out = self.clear(api, minutes=60, now=lambda: next(times))
        self.assertEqual(status, 1)
        self.assertEqual(api.job_polls, [])
        self.assertEqual(api.deleted, [])
        self.assertIn('no Post Setup step started within 60', out)

    def test_only_the_save_step_counts_as_the_save(self):
        polls = [[warm('in_progress', step('Post Upload logs', 'in_progress'), step('Post Setup', 'pending'))],
                 [warm('completed', step('Post Upload logs', 'completed', 'success'), step('Post Setup', 'completed', 'skipped'),
                       conclusion='success')]]
        api = ClearApi(self.entries(), polls)
        self.assertEqual(self.clear(api)[0], 1)
        self.assertEqual(api.deleted, [])

    def test_a_job_that_ends_before_its_save_deletes_nothing_and_fails(self):
        for conclusion in ('failure', 'cancelled', 'success'):
            with self.subTest(conclusion):
                ending = warm('completed', step('Post Setup', 'completed', 'skipped'), conclusion=conclusion)
                api = ClearApi(self.entries(), [[ending]])
                self.assertEqual(self.clear(api)[0], 1)
                self.assertEqual(api.deleted, [])

    def test_an_attempt_without_the_saving_job_asks_the_earlier_attempt(self):
        others = [job(7, 'cache prune', 'completed')]
        saved = warm('completed', step('Post Setup', 'completed', 'success'), conclusion='success')
        failed = warm('completed', step('Post Setup', 'completed', 'failure'), conclusion='failure')
        for name, earlier, listed, expected in (('saved', saved, True, 0), ('nothing listed', saved, False, 1),
                                                ('failed', failed, True, 1)):
            with self.subTest(name):
                entries = self.entries() + ([entry(9, SAVED, '10:40')] if listed else [])
                api = ClearApi(entries, [others, others], earlier={1: [earlier]})
                self.assertEqual(self.clear(api, attempt='2')[0], expected)
                self.assertEqual(api.deleted, [])
        api = ClearApi(self.entries(), [others, others])
        self.assertEqual(self.clear(api)[0], 1)  # the jobs it needs stopped, so it never ran

    def test_an_earlier_attempt_with_two_matching_jobs_fails(self):
        others = [job(7, 'cache prune', 'completed')]
        saved = warm('completed', step('Post Setup', 'completed', 'success'), conclusion='success')
        windows = dict(saved, id=6, name='gate / cache warm (windows-latest)')
        api = ClearApi(self.entries() + [entry(9, SAVED, '10:40')], [others, others], earlier={1: [saved, windows]})
        status, out = self.clear(api, attempt='2')
        self.assertEqual((status, api.deleted), (1, []))
        self.assertIn('matches 2 jobs', out)

    def test_an_entry_newer_than_the_jobs_start_stays_in_a_later_attempt(self):
        # "Re-run all jobs" lists the leg of attempt 1 again, already saved; the run's start is now
        # later than the entry it saved.
        api = ClearApi(self.entries(), [[warm('completed', step('Post Setup', 'completed', 'success'), conclusion='success',
                                              started='09:00')]])
        api.run = lambda run: {'run_started_at': '2026-11-19T12:00:00Z'}
        api.entries.append(entry(9, SAVED, '09:20'))
        status, _ = self.clear(api, attempt='2')
        self.assertEqual(status, 0)
        self.assertEqual(api.deleted, [])

    def test_dry_run_deletes_nothing(self):
        api = ClearApi(self.entries(), [[warm('in_progress', step('Post Setup', 'completed', 'success'))]],
                       late=[(1, entry(9, SAVED, '10:40'))])
        status, out = self.clear(api, dry_run=True)
        self.assertEqual((status, api.deleted), (0, []))
        self.assertIn(f"would delete {crates('100')}", out)

    def test_every_page_of_jobs_and_caches_is_read(self):
        api = prune.Api('o/r', 't', url='https://api.example')
        deleted = []
        others = [{'id': 100 + n, 'name': f'test {n}', 'status': 'completed', 'runner_name': 'r'} for n in range(100)]
        filler = [entry(100 + n, f'apt-debs-{n}', '08:00') for n in range(100)]
        lists = {'jobs': [others, [warm('in_progress', step('Post Setup', 'completed', 'success'))]],
                 'actions_caches': [filler, [entry(1, crates('100'), '09:00'), entry(9, SAVED, '10:40')]]}

        def request(method, path):
            if method == 'DELETE':
                deleted.append(int(path.rsplit('/', 1)[1]))
                return None
            if path.endswith('/runs/1'):
                return {'run_started_at': '2026-11-19T09:30:00Z'}
            field = 'jobs' if '/jobs' in path else 'actions_caches'
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['page'][0])
            return {field: lists[field][page - 1]}
        with unittest.mock.patch.object(api, 'request', side_effect=request):
            status, _ = self.clear(api)
        self.assertEqual((status, deleted), (0, [1]))


class DefinitionTests(unittest.TestCase):
    def test_the_action_runs_the_script_with_its_inputs(self):
        action = yaml.load((ROOT / 'actions/cache-prune/action.yml').read_text(), Loader=yaml.BaseLoader)
        env = action['runs']['steps'][0]['env']
        self.assertEqual(env['GH_TOKEN'], '${{ inputs.token }}')
        self.assertEqual(action['inputs']['watch']['default'], 'false')
        self.assertEqual(action['inputs']['dry-run']['default'], 'false')
        self.assertEqual(action['inputs']['clear-rolling-on-save']['default'], '')
        for name in ('clear-rolling-on-save', 'rolling-family', 'save-step'):
            self.assertEqual(env[f"PRUNE_{name.upper().replace('-', '_')}"], f'${{{{ inputs.{name} }}}}')
        self.assertNotIn('${{', action['runs']['steps'][0]['run'])

    def test_the_default_save_step_is_the_gate_legs_setup_post_step(self):
        # A post step is listed as `Post <step name>`; gate.yml's saving legs set up in `Setup`.
        action = yaml.load((ROOT / 'actions/cache-prune/action.yml').read_text(), Loader=yaml.BaseLoader)
        gate = yaml.load((ROOT / '.github/workflows/gate.yml').read_text(), Loader=yaml.BaseLoader)
        for name in ('build', 'gate'):
            with self.subTest(name):
                setup = [s for s in gate['jobs'][name]['steps'] if 'actions/gate-setup@' in s.get('uses', '')]
                self.assertEqual([f"Post {s['name']}" for s in setup], [action['inputs']['save-step']['default']])
                tolerated = {s['name'] for s in gate['jobs'][name]['steps'] if s.get('continue-on-error') == 'true'}
                self.assertEqual(tolerated, set(prune.TOLERATED))


if __name__ == '__main__':
    unittest.main()

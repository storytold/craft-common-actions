"""Deletes the Actions cache entries of one branch that no run will restore again.

Families:
- rolling `cache-workspace-crates` entries (`<v>-rust-<profile>-<job>-crates-<os>-<arch>-<n>-<n>-...`):
  a run restores only the newest, so every other one goes at once;
- other rust-cache entries (`...-rust-...-<os>-<arch>-<environment hash>-<lockfile hash>`), and
  `<name>-<sha256>` entries for the names given: the newest stays, and an older one goes once nothing
  has used it since the newest was created (a run that still restores it keeps it). The family
  leaves out the environment and lockfile hashes, so that a toolchain or lockfile update replaces
  the old entry; two jobs that share a key name but differ in toolchain or environment would count
  as one family.
A rolling entry also replaces the other rust-cache entries of the same job, OS and architecture
(`<v>-rust-<profile>-<job>-<os>-<arch>-...`, written before the job turned `cache-workspace-crates`
on): each goes once nothing has used it since the newest rolling entry was created. A plain entry
never replaces a rolling one.
Everything else stays.
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CRATES = re.compile(r'^(?P<prefix>.+?)-rust-[0-9a-f]{64}-(?P<entry>.+-crates-[A-Za-z_]+-[a-z0-9]+)-(?P<run>[0-9]+)-(?P<attempt>[0-9]+)-')
RUST = re.compile(r'^(?P<name>.+-rust-.+)-(?P<os>Linux|Windows_NT|Darwin)-(?P<arch>[a-z0-9]+)-[0-9a-f]{8}-[0-9a-f]{8}$')
PROFILE = re.compile(r'-[0-9a-f]{64}(?=-)')
POLL_SECONDS = 30
# Polls after the last saving job, while the cache list does not show an entry it saved yet.
FINAL_POLLS = 3


def family(key, content_names):
    """(rule, family) for a key, or None for an entry this never deletes."""
    m = CRATES.match(key)
    if m:
        return 'rolling', f"{m['prefix']}-rust-{m['entry']}"
    m = RUST.match(key)
    if m:
        return 'replaced', f"{PROFILE.sub('', m['name'])}-{m['os']}-{m['arch']}"
    for name in content_names:
        if re.fullmatch(re.escape(name) + r'-[0-9a-f]{64}', key):
            return 'replaced', name
    return None


def plain_family(rolling):
    """The family of the job's other rust-cache entries, for a rolling family's name."""
    head, _, tail = rolling.rpartition('-crates-')
    return 'replaced', f'{head}-{tail}'


def when(timestamp):
    return datetime.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))


def plan(entries, content_names):
    """The entries to delete, each with the reason, newest family members first."""
    families = {}
    for entry in entries:
        found = family(entry['key'], content_names)
        if found:
            families.setdefault(found, []).append(entry)
    deletions = []
    for (rule, _), members in families.items():
        members.sort(key=lambda e: when(e['created_at']), reverse=True)
        newest = members[0]
        for entry in members[1:]:
            if rule == 'rolling':
                deletions.append((entry, f"older than {newest['key']}"))
            elif when(entry['last_accessed_at']) < when(newest['created_at']):
                deletions.append((entry, f"replaced by {newest['key']} and unused since"))
    planned = {entry['id'] for entry, _ in deletions}
    for (rule, name), members in families.items():
        if rule != 'rolling':
            continue
        newest = members[0]
        for entry in families.get(plain_family(name), []):
            if entry['id'] not in planned and when(entry['last_accessed_at']) < when(newest['created_at']):
                deletions.append((entry, f"replaced by {newest['key']} and unused since"))
    return deletions


def gb(n):
    return f'{n / 1e9:.2f} GB'


class Api:
    def __init__(self, repo, token, url=None):
        self.repo = repo
        self.token = token
        self.url = (url or os.environ.get('GITHUB_API_URL') or 'https://api.github.com').rstrip('/')

    def request(self, method, path):
        request = urllib.request.Request(f'{self.url}{path}', method=method, headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {self.token}',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        return json.loads(body) if body else None

    def pages(self, path, field):
        items, page = [], 1
        while True:
            separator = '&' if '?' in path else '?'
            batch = self.request('GET', f'{path}{separator}per_page=100&page={page}')[field]
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def caches(self, ref):
        query = urllib.parse.urlencode({'ref': ref, 'sort': 'created_at', 'direction': 'desc'})
        return self.pages(f'/repos/{self.repo}/actions/caches?{query}', 'actions_caches')

    def delete(self, cache_id):
        try:
            self.request('DELETE', f'/repos/{self.repo}/actions/caches/{cache_id}')
        except urllib.error.HTTPError as error:
            if error.code != 404:  # already deleted by an overlapping run's prune
                raise

    def jobs(self, run, attempt):
        return self.pages(f'/repos/{self.repo}/actions/runs/{run}/attempts/{attempt}/jobs', 'jobs')

    def run(self, run):
        return self.request('GET', f'/repos/{self.repo}/actions/runs/{run}')


def prune(api, ref, content_names, dry_run):
    entries = api.caches(ref)
    deletions = plan(entries, content_names)
    for entry, reason in deletions:
        print(f"{'would delete' if dry_run else 'delete'} {entry['key']} ({gb(entry['size_in_bytes'])}): {reason}")
        if not dry_run:
            api.delete(entry['id'])
    deleted = {entry['id'] for entry, _ in deletions}
    left = sum(e['size_in_bytes'] for e in entries if e['id'] not in deleted)
    print(f'{ref}: {len(entries) - len(deleted)} entries, {gb(left)} after this pass')
    return entries


def try_prune(api, ref, content_names, dry_run):
    """A pass that warns instead of failing: the next pass, or the next run's, deletes what it missed."""
    try:
        return prune(api, ref, content_names, dry_run)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        print(f'::warning::cache-prune: a pass failed ({error!r}); the next pass tries again')
        return None


def saved_since(entries, content_names, started):
    """The list shows an entry of a pruned family created after `started` (a job's start time)."""
    return any(family(e['key'], content_names) and when(e['created_at']) >= when(started) for e in entries)


def finished(jobs, patterns):
    """Every saving job has been listed and has completed (a skipped one is listed as completed)."""
    return all(any(p.search(j['name']) for j in jobs)
               and all(j['status'] == 'completed' for j in jobs if p.search(j['name']))
               for p in patterns)


def other_jobs(api, run, attempt, me):
    """The attempt's jobs without this one, or None (with a warning) when listing them failed."""
    try:
        return [j for j in api.jobs(run, attempt) if not (me and j.get('runner_name') == me and j['status'] != 'completed')]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        print(f'::warning::listing the run\'s jobs failed ({error!r}); trying again in {POLL_SECONDS} s')
        return None


def watch(api, ref, content_names, dry_run, run, attempt, patterns, me, minutes,
          sleep=time.sleep, clock=time.monotonic):
    """Prunes once now and again after each saving job finishes; returns when none is left.

    Jobs are listed only once their `needs` are met, so a saving job that has not been listed yet
    keeps the watch going. With no patterns it ends once no other job has been queued or running
    for two polls in a row, which also ends a re-run attempt that runs none of the saving jobs.
    After the last saving job, the cache list may not show its entry yet: the watch prunes again
    every poll until the list holds an entry created after that job started, at most FINAL_POLLS
    times (a job whose key already existed saves nothing). It ends after `minutes` in any case,
    with one more pass, so that it stops before the job's own timeout.
    """
    deadline = clock() + minutes * 60
    try_prune(api, ref, content_names, dry_run)
    seen, idle = set(), 0
    while True:
        if clock() >= deadline:
            print(f'::warning::cache-prune: still watching after {minutes} minutes; one last pass')
            try_prune(api, ref, content_names, dry_run)
            return
        sleep(POLL_SECONDS)
        jobs = other_jobs(api, run, attempt, me)
        if jobs is None:
            continue
        relevant = [j for j in jobs if any(p.search(j['name']) for p in patterns)] if patterns else jobs
        done_now = [j for j in relevant if j['status'] == 'completed' and j['id'] not in seen]
        entries = None
        if done_now:
            seen.update(j['id'] for j in done_now)
            print(f"finished: {', '.join(j['name'] for j in done_now)}")
            entries = try_prune(api, ref, content_names, dry_run)
        idle = idle + 1 if all(j['status'] == 'completed' for j in jobs) else 0
        if patterns and finished(jobs, patterns):
            ran = [j['started_at'] for j in done_now if j.get('started_at') and j.get('conclusion') not in ('skipped', 'cancelled')]
            if ran:
                started = max(ran, key=when)
                for _ in range(FINAL_POLLS):
                    if entries is not None and saved_since(entries, content_names, started):
                        break
                    sleep(POLL_SECONDS)
                    entries = try_prune(api, ref, content_names, dry_run)
            return
        if idle >= 2:
            return


STOPPED = ('failure', 'cancelled', 'timed_out')
# gate.yml's steps with continue-on-error in the jobs that save: their failure leaves the job's
# status, and so the save, unchanged.
TOLERATED = ('Trim the cache entry to this build',)


def save_steps(job, step):
    return [s for s in job.get('steps') or [] if s['name'] == step]


def saving(job, step):
    """The job's save step is running or has run (a skipped one has not)."""
    return any(s['status'] == 'in_progress' or (s['status'] == 'completed' and s.get('conclusion') != 'skipped')
               for s in save_steps(job, step))


def failed_step(job, step):
    """The first step before the save step that failed or was canceled, post steps included, or
    None. rust-cache does not save after a failed step (gate-setup sets `cache-on-failure: false`),
    even though the post step that holds its save still runs the other post actions."""
    for s in job.get('steps') or []:
        if s['name'] == step:
            return None
        if s['name'] not in TOLERATED and s.get('conclusion') in STOPPED:
            return s['name']
    return None


def save_outcome(job, step):
    """'success' once the save step succeeded, why the save did not succeed, or None while it runs."""
    steps = save_steps(job, step)
    if steps and all(s['status'] == 'completed' for s in steps):
        failed = next((s.get('conclusion') for s in steps if s.get('conclusion') != 'success'), None)
        return f'the {step} step ended {failed}' if failed else 'success'
    if job['status'] == 'completed':
        return f"the job ended {job.get('conclusion')} before its {step} step finished"
    return None


def overdue(job, began, minutes, now):
    """`minutes` have passed since the job started, or since `began` while it has not started."""
    started = job.get('started_at') if job and job['status'] in ('in_progress', 'completed') else None
    return now() - (when(started) if started else began) > datetime.timedelta(minutes=minutes)


def rolling_before(entries, rolling, moment):
    """The entries of the rolling family created before `moment`."""
    return [e for e in entries if family(e['key'], []) == ('rolling', rolling) and when(e['created_at']) < moment]


def saved_entry(api, ref, rolling, run, job, sleep, polls):
    """The entry the job saved, or None: a cold build's key ends its rolling family with the job's
    `<run>-<attempt>` (actions/rust-cache, Run entry). Lists again up to `polls` more times while the
    list does not show it yet."""
    suffix = (str(run), str(job.get('run_attempt') or ''))
    for poll in range(polls + 1):
        try:
            for entry in api.caches(ref):
                m = CRATES.match(entry['key'])
                if m and family(entry['key'], []) == ('rolling', rolling) and (m['run'], m['attempt']) == suffix:
                    return entry
        except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
            print(f'::warning::listing the cache entries failed ({error!r})')
        if poll < polls:
            sleep(POLL_SECONDS)
    return None


def earlier_save(api, ref, rolling, run, attempt, pattern, step, sleep):
    """The exit status when the saving job is not part of this attempt: 0 when the latest earlier
    attempt that ran it saved its entry."""
    for earlier in range(int(attempt) - 1, 0, -1):
        matching = [j for j in api.jobs(run, earlier) if pattern.search(j['name'])]
        if len(matching) > 1:
            print(f"::error::{pattern.pattern} matches {len(matching)} jobs in attempt {earlier} "
                  f"({', '.join(j['name'] for j in matching)}); name the one that saves the rolling entry. Nothing deleted")
            return 1
        job = matching[0] if matching else None
        if job:
            outcome = save_outcome(job, step)
            if outcome == 'success' and saved_entry(api, ref, rolling, run, job, sleep, 0):
                print(f"{job['name']} saved its entry in attempt {earlier}; nothing deleted")
                return 0
            outcome = 'no entry listed' if outcome == 'success' else outcome or 'still running'
            print(f"::error::{job['name']} did not save in attempt {earlier} ({outcome}) "
                  'and is not part of this attempt; nothing deleted')
            return 1
    print(f'::error::no job matching {pattern.pattern} ran in this attempt; nothing deleted')
    return 1


def clear_rolling(api, ref, rolling, run, attempt, pattern, step, dry_run, me, minutes,
                  sleep=time.sleep, now=lambda: datetime.datetime.now(datetime.timezone.utc)):
    """For a run that restores no rolling entry: deletes the rolling family's older entries once the
    saving job's save step starts, and returns 0 only when that save then succeeds.

    The deleted entries are those created before the run, or the saving job, started, so an entry
    another run saved meanwhile stays unless it is older than the job. The wait for the save, and for
    its result, ends `minutes` after the saving job started (after this started, while the job has
    not), and a save first seen after that deletes nothing. A job that ends without its save step
    deletes nothing and fails. When the job is not part of this
    attempt and no other job is queued or running for two polls, the latest earlier attempt that ran
    it decides the result. After the step succeeds, the cache list must show the entry the job saved
    (at most FINAL_POLLS more polls). Returns 1 with an error when the
    save did not run, did not succeed or saved no entry; after a deletion the branch then holds no
    rolling entry of the family, and the next run starts cold.
    """
    began, idle, job = now(), 0, None
    while True:
        jobs = other_jobs(api, run, attempt, me)
        if jobs is not None:
            matching = [j for j in jobs if pattern.search(j['name'])]
            if len(matching) > 1:
                print(f"::error::{pattern.pattern} matches {len(matching)} jobs ({', '.join(j['name'] for j in matching)}); "
                      'name the one that saves the rolling entry. Nothing deleted')
                return 1
            job = matching[0] if matching else None
            if job and saving(job, step):
                if overdue(job, began, minutes, now):
                    print(f"::error::{job['name']}'s {step} step was first seen more than {minutes} minutes after "
                          'the job started; nothing deleted')
                    return 1
                break
            if job and job['status'] == 'completed':
                print(f"::error::{job['name']} ended {job.get('conclusion')} without its {step} step; nothing deleted")
                return 1
            idle = idle + 1 if job is None and all(j['status'] == 'completed' for j in jobs) else 0
            if idle >= 2:
                return earlier_save(api, ref, rolling, run, attempt, pattern, step, sleep)
        if overdue(job, began, minutes, now):
            print(f'::error::no {step} step started within {minutes} minutes; nothing deleted')
            return 1
        sleep(POLL_SECONDS)
    outcome = save_outcome(job, step)
    if outcome not in (None, 'success'):
        print(f'::error::{outcome}: no new {rolling} entry was saved; nothing deleted')
        return 1
    failed = failed_step(job, step)
    if failed:
        print(f"::error::{job['name']}: the step {failed!r} failed, so the {step} step saves no entry; nothing deleted")
        return 1
    cutoff = min(when(api.run(run)['run_started_at']), when(job['started_at']))
    old = rolling_before(api.caches(ref), rolling, cutoff)
    for entry in old:
        print(f"{'would delete' if dry_run else 'delete'} {entry['key']} ({gb(entry['size_in_bytes'])}): "
              f"{job['name']} is saving its successor")
        if not dry_run:
            api.delete(entry['id'])
    while True:
        outcome = save_outcome(job, step)
        if outcome is not None:
            break
        if overdue(job, began, minutes, now):
            print(f'::error::the {step} step is still running {minutes} minutes after {job["name"]} started')
            return 1
        sleep(POLL_SECONDS)
        jobs = other_jobs(api, run, attempt, None)
        job = next((j for j in jobs or [] if j['id'] == job['id']), job)
    gone = ', and the next run starts cold' if old and not dry_run else ''
    if outcome != 'success':
        print(f'::error::{outcome}: no new {rolling} entry was saved{gone}')
        return 1
    new = saved_entry(api, ref, rolling, run, job, sleep, FINAL_POLLS)
    if new:
        print(f"saved {new['key']} ({gb(new['size_in_bytes'])})")
        return 0
    print(f"::error::the {step} step succeeded, but the cache list shows no {rolling} entry that "
          f"{job['name']} saved{gone}")
    return 1


def main():
    env = os.environ
    api = Api(env['PRUNE_REPO'], env['GH_TOKEN'])
    ref = env['PRUNE_REF']
    content_names = env.get('PRUNE_CONTENT_KEYS', '').split()
    dry_run = env.get('PRUNE_DRY_RUN', 'false') == 'true'
    try:
        clear = env.get('PRUNE_CLEAR_ROLLING_ON_SAVE', '').strip()
        if clear:
            return clear_rolling(api, ref, env.get('PRUNE_ROLLING_FAMILY') or 'v2-rust-gate-crates-Linux-x64',
                                 env['PRUNE_RUN'], env['PRUNE_ATTEMPT'], re.compile(clear),
                                 env.get('PRUNE_SAVE_STEP') or 'Post Setup', dry_run, env.get('RUNNER_NAME'),
                                 float(env.get('PRUNE_WATCH_MINUTES') or 110))
        if env.get('PRUNE_WATCH', 'false') == 'true':
            patterns = [re.compile(line.strip()) for line in env.get('PRUNE_SAVING_JOBS', '').splitlines() if line.strip()]
            watch(api, ref, content_names, dry_run, env['PRUNE_RUN'], env['PRUNE_ATTEMPT'], patterns,
                  env.get('RUNNER_NAME'), float(env.get('PRUNE_WATCH_MINUTES') or 110))
        else:
            prune(api, ref, content_names, dry_run)
    except Exception as error:  # noqa: BLE001 - a missed pass leaves an entry for the next run's prune
        print(f'::warning::cache-prune stopped: {error!r}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

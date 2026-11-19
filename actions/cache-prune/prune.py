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

CRATES = re.compile(r'^(?P<prefix>.+?)-rust-[0-9a-f]{64}-(?P<entry>.+-crates-[A-Za-z_]+-[a-z0-9]+)-[0-9]+-[0-9]+-')
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
        try:
            jobs = [j for j in api.jobs(run, attempt) if not (me and j.get('runner_name') == me and j['status'] != 'completed')]
        except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
            print(f'::warning::listing the run\'s jobs failed ({error!r}); trying again in {POLL_SECONDS} s')
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


def main():
    env = os.environ
    api = Api(env['PRUNE_REPO'], env['GH_TOKEN'])
    ref = env['PRUNE_REF']
    content_names = env.get('PRUNE_CONTENT_KEYS', '').split()
    dry_run = env.get('PRUNE_DRY_RUN', 'false') == 'true'
    try:
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

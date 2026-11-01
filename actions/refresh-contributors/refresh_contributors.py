#!/usr/bin/env python3
"""Refresh release credits using local Git statistics and authenticated GitHub identities.

Requires Python 3.11+, Git and gh. The existing snapshot is replaced atomically only
after all collection and consent checks succeed; no commit or push is performed.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib


def run(args, cwd, input_text=None):
    """Run a bounded, non-shell command and report its failure with context."""
    result = subprocess.run(args, cwd=cwd, input=input_text.encode("utf-8") if input_text is not None else None,
                            capture_output=True, timeout=600)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.decode('utf-8', errors='replace').strip()}")
    return result.stdout.decode("utf-8")


def api(root, endpoint, query, paginate=False):
    """Read projected JSON records through gh without exposing its credentials."""
    args = ["gh", "api", endpoint, "--jq", query]
    if paginate:
        args.append("--paginate")
    return [json.loads(line) for line in run(args, root).splitlines() if line.strip()]


def commit_authors(root, repo, head):
    """Page GitHub's head-bound history, including verified human co-author identities."""
    owner, name = repo.split("/")
    query = """query($owner: String!, $name: String!, $head: String!, $cursor: String) {
      repository(owner: $owner, name: $name) { object(expression: $head) { ... on Commit {
        history(first: 100, after: $cursor) {
          nodes { oid author { user { login } }
            authors(first: 100) { nodes { user { login } } pageInfo { hasNextPage } }
          }
          pageInfo { hasNextPage endCursor }
        }
      } } }
    }"""
    identities, authors, seen_cursors = {}, {}, set()
    cursor = None
    while True:
        body = json.dumps(dict(query=query, variables=dict(owner=owner, name=name, head=head, cursor=cursor)))
        data = json.loads(run(["gh", "api", "graphql", "--input", "-"], root, input_text=body))
        page = data["data"]["repository"]["object"]["history"]
        for node in page["nodes"]:
            sha = node["oid"]
            if sha in identities:
                raise ValueError(f"GitHub returned a duplicate history commit: {sha}")
            if node["authors"]["pageInfo"]["hasNextPage"]:
                raise ValueError(f"More than 100 authors on {sha}; refusing incomplete credits")
            primary = node["author"]["user"]
            identities[sha] = primary["login"] if primary else None
            accounts = [actor["user"]["login"] for actor in node["authors"]["nodes"] if actor["user"]]
            if primary:
                accounts.append(primary["login"])
            if any(not isinstance(login, str) or not login for login in accounts):
                raise ValueError(f"Invalid GitHub account identity on {sha}")
            authors[sha] = {login.casefold(): login for login in accounts}
        if not page["pageInfo"]["hasNextPage"]:
            return identities, authors
        cursor = page["pageInfo"]["endCursor"]
        if not cursor or cursor in seen_cursors:
            raise ValueError("GitHub history pagination did not advance")
        seen_cursors.add(cursor)


def single_value(root, endpoint, query):
    """Require exactly one scalar record from an API projection."""
    records = api(root, endpoint, "{value: " + query + "}")
    if len(records) != 1 or not isinstance(records[0], dict) or "value" not in records[0]:
        raise ValueError(f"Expected one API value from {endpoint}")
    return records[0]["value"]


def load_people(root, head="HEAD"):
    """Read and validate name preferences from the selected immutable commit."""
    people = tomllib.loads(run(["git", "show", f"{head}:contributors/people.toml"], root)).get("people", {})
    if not isinstance(people, dict):
        raise ValueError("people.toml must contain people tables")
    seen = set()
    for login, entry in people.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login) or not isinstance(entry, dict):
            raise ValueError("people.toml contains an invalid GitHub username/table")
        if login.casefold() in seen:
            raise ValueError(f"people.toml contains duplicate account casing for {login}")
        seen.add(login.casefold())
        for field in ("display_name", "real_name", "consent_source"):
            if field in entry and not isinstance(entry[field], str):
                raise ValueError(f"people.{login}.{field} must be a string")
        if "sync_github_name" in entry and not isinstance(entry["sync_github_name"], bool):
            raise ValueError(f"people.{login}.sync_github_name must be a boolean")
    return people


def utc_date(value):
    """Normalize an ISO commit date with the standard library calendar implementation."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def history(root, head):
    """Read full ancestry and per-commit text/binary statistics without following renames."""
    text = run(["git", "-c", "core.quotePath=true", "log", "--no-show-signature",
                "--root", "--no-renames", "--raw", "--numstat",
                "--format=%x00%H%x00%aI%x00%(trailers:key=Co-authored-by,valueonly,separator=%x1f)%x00",
                head, "--"], root)
    fields = text.split("\0")
    if (len(fields) - 1) % 4:
        raise ValueError("Unexpected Git history format")
    commits = {}
    for index in range(1, len(fields), 4):
        sha, date, trailers, changes = fields[index:index + 4]
        statuses, stats = [], []
        for line in changes.splitlines():
            if line.startswith(":"):
                statuses.append(line.split("\t", 1)[0].split()[-1])
            elif "\t" in line:
                added, deleted, _ = line.split("\t", 2)
                stats.append((added, deleted))
        if len(statuses) != len(stats):
            raise ValueError(f"Git file statistics do not match for {sha}")
        counts = dict(lines_added=0, lines_deleted=0, binary_added=0, binary_deleted=0)
        for status, (added, deleted) in zip(statuses, stats):
            if added == deleted == "-":
                counts["binary_added"] += status == "A"
                counts["binary_deleted"] += status == "D"
            else:
                counts["lines_added"] += int(added)
                counts["lines_deleted"] += int(deleted)
        commits[sha] = dict(date=utc_date(date), trailers=trailers.split("\x1f"), **counts)
    return commits


def model(trailer):
    """Recognize explicit model co-author trailers, without inferring a model from humans."""
    match = re.fullmatch(r"\s*(.*?)\s*<([^<>]+)>\s*", trailer)
    if not match:
        return None
    name, email = match.groups()
    if email.casefold() == "noreply@anthropic.com":
        match = re.fullmatch(r"Claude(?: (Opus|Sonnet|Haiku|Fable))?(?: (\d+(?:\.\d+)*))?(?: \([^)]*\))?", name)
        if match:
            family, version = match.groups()
            title = "Claude" + (f" {family}" if family else "")
            return dict(id="anthropic/" + title.lower().replace(" ", "-") + (f"/{version}" if version else ""),
                        company="Anthropic", model=title, version=version or "")
    if email.casefold() in ("codex@openai.com", "noreply@openai.com"):
        match = re.fullmatch(r"(?:OpenAI )?(Codex|ChatGPT|GPT)(?:[- ](\d+(?:\.\d+)*))?", name)
        if match:
            family, version = match.groups()
            return dict(id="openai/" + family.lower() + (f"/{version}" if version else ""),
                        company="OpenAI", model=family, version=version or "")
    return None


def people_authors(root, head="HEAD"):
    """Find the commits responsible for each table's content, not Git author names/emails."""
    text = run(["git", "blame", "--line-porcelain", head, "--", "contributors/people.toml"], root)
    result, sha, active = {}, None, None
    for line in text.splitlines():
        if re.match(r"^[0-9a-f]{40,64} \d+ \d+", line):
            sha = line.split()[0]
        elif line.startswith("\t"):
            value = line[1:].strip()
            if value.startswith("["):
                table = tomllib.loads(value).get("people", {})
                active = next(iter(table), None)
                continue
            if active and value and not value.startswith("#"):
                result.setdefault(active, set()).add(sha)
    return result


def verified_people(root, repo, people, identities, pull_authors, head="HEAD"):
    """Verify self-authored entries or recorded consent committed by a repository writer."""
    blame = people_authors(root, head)
    permissions = {}
    verified = {}
    for login, entry in people.items():
        allowed = True
        sources = blame.get(login, set())
        for sha in sources:
            actor = identities.get(sha) or pull_authors.get(sha)
            if actor and actor.casefold() == login.casefold():
                continue
            if not actor or not isinstance(entry.get("consent_source"), str) or not entry["consent_source"].strip():
                allowed = False
                break
            if actor not in permissions:
                permissions[actor] = single_value(root, f"repos/{repo}/collaborators/{actor}/permission", ".permission")
            if permissions[actor] not in ("admin", "write"):
                allowed = False
                break
        verified[login.casefold()] = bool(sources) and allowed
    return verified


def collect(root, repo):
    """Collect a complete, head-bound snapshot; fail before writing on API/history errors."""
    if run(["git", "rev-parse", "--is-shallow-repository"], root).strip() != "false":
        raise ValueError("Contributor refresh requires full Git history (fetch-depth: 0)")
    head = run(["git", "rev-parse", "HEAD"], root).strip()
    try:
        run(["git", "diff", "--quiet", head, "--", "contributors/people.toml"], root)
    except RuntimeError as error:
        raise ValueError("Contributor preferences must match the release commit; "
                         f"commit or restore contributors/people.toml ({error})") from error
    commits = history(root, head)
    identities, authors = commit_authors(root, repo, head)
    missing = commits.keys() - identities.keys()
    if missing:
        raise ValueError(f"GitHub identity response is missing {len(missing)} reachable commits")
    pulls = api(root, f"repos/{repo}/pulls?state=closed&per_page=100",
                ".[] | select(.merged_at != null) | {number, sha: .merge_commit_sha, login: .user.login}",
                paginate=True)
    pull_authors = {}
    for pull in pulls:
        if pull["sha"] in commits:
            pull_authors.setdefault(pull["sha"], set()).add(pull["login"])
    # Only an unambiguous merged PR can supply an otherwise unknown commit identity.
    pull_authors = {sha: next(iter(logins)) for sha, logins in pull_authors.items() if len(logins) == 1}
    people = load_people(root, head)
    verified = verified_people(root, repo, people, identities, pull_authors, head)
    people_logins = {login.casefold(): login for login in people}
    people = {login.casefold(): entry for login, entry in people.items()}
    contributors, models = {}, {}

    def contributor(login):
        """Create one username row without copying any human name into the registry."""
        return contributors.setdefault(login.casefold(), dict(
            login=people_logins.get(login.casefold(), login),
            people_entry=verified.get(login.casefold(), False), github_name=None,
            prs=0, commits=0, lines_added=0, lines_deleted=0, binary_added=0, binary_deleted=0,
            first_commit="", last_commit=""))

    unattributed = 0
    for sha, commit in commits.items():
        logins = authors[sha] or {login.casefold(): login for login in [pull_authors.get(sha)] if login}
        if not logins:
            unattributed += 1
        for login in logins.values():
            row = contributor(login)
            row["commits"] += 1
            for field in ("lines_added", "lines_deleted", "binary_added", "binary_deleted"):
                row[field] += commit[field]
            row["first_commit"] = min(row["first_commit"] or commit["date"], commit["date"])
            row["last_commit"] = max(row["last_commit"], commit["date"])
        seen = set()
        for trailer in commit["trailers"]:
            identity = model(trailer.strip())
            if identity and identity["id"] not in seen:
                seen.add(identity["id"])
                row = models.setdefault(identity["id"], dict(**identity, commits=0, lines_added=0, lines_deleted=0))
                row["commits"] += 1
                row["lines_added"] += commit["lines_added"]
                row["lines_deleted"] += commit["lines_deleted"]
    seen_pulls = set()
    for pull in pulls:
        if pull["sha"] in commits and pull["number"] not in seen_pulls and pull["login"]:
            seen_pulls.add(pull["number"])
            contributor(pull["login"])["prs"] += 1
    for row in contributors.values():
        entry = people.get(row["login"].casefold(), {})
        if row["people_entry"] and entry.get("sync_github_name") is True:
            row["github_name"] = single_value(root, f"users/{row['login']}", ".name")
    if run(["git", "rev-parse", "HEAD"], root).strip() != head:
        raise ValueError("HEAD changed during contributor collection; retry on a fixed commit")
    if not contributors:
        raise ValueError("Contributor snapshot is empty")
    return dict(schema=1, repo=repo,
                branch=os.environ.get("GITHUB_REF_NAME") or run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root).strip(),
                head=head, head_date=utc_date(run(["git", "show", "-s", "--format=%cI", head], root).strip()),
                commits=len(commits), unattributed_commits=unattributed,
                contributors=sorted(contributors.values(), key=lambda row: (-row["commits"], row["login"].casefold())),
                models=sorted(models.values(), key=lambda row: (-row["commits"], row["id"])))


def write_snapshot(path, snapshot):
    """Replace the old snapshot atomically, retaining it if writing or replacement fails."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".contributors-", delete=False) as output:
            temporary = Path(output.name)
            json.dump(snapshot, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    """Refresh only the generated snapshot in the selected caller checkout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub owner/repository for the release checkout")
    parser.add_argument("--directory", type=Path, default=Path.cwd(), help="App checkout (default: current directory)")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be a GitHub owner/repository")
    try:
        root = args.directory.resolve()
        snapshot = collect(root, args.repo)
        write_snapshot(root / "contributors/contributors.json", snapshot)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"Contributor refresh failed: {error}", file=sys.stderr)
        return 1
    print(f"Refreshed {len(snapshot['contributors'])} contributors for {snapshot['head']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

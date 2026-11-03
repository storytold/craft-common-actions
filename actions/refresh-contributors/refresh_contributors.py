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


# AI agents identified by their exact Git email or GitHub account: (company, id prefix, default
# model, read the model and version from the Git name). Agents are credited as models, never people.
AGENT_EMAILS = {
    "noreply@anthropic.com": ("Anthropic", "anthropic", "Claude", True),
    "codex@openai.com": ("OpenAI", "openai", "Codex", True),
    "noreply@openai.com": ("OpenAI", "openai", "Codex", True),
    "cursoragent@cursor.com": ("Anysphere", "anysphere", "Cursor", False),
    "noreply@deepseek.com": ("DeepSeek", "deepseek", "DeepSeek", True),
    "noreply@z.ai": ("Zhipu AI", "zhipu", "GLM", True),
    "noreply@zcode.ai": ("Zhipu AI", "zhipu", "GLM", True),
    "noreply@qwen.ai": ("Alibaba", "alibaba", "Qwen", True),
    "noreply@google.com": ("Google", "google", "Gemini", True),
    "noreply@minimax.io": ("MiniMax", "minimax", "MiniMax", True),
    "noreply@x.ai": ("xAI", "xai", "Grok", True),
    "noreply@xiaomi.com": ("Xiaomi", "xiaomi", "MiMo", False),
    "noreply@opencode.ai": ("OpenCode", "opencode", "OpenCode", True),
    "noreply@codebuff.com": ("Codebuff", "codebuff", "Codebuff", False),
    "noreply@paperclip.ing": ("Paperclip", "paperclip", "Paperclip", False),
}
AGENT_LOGINS = {
    "claude": AGENT_EMAILS["noreply@anthropic.com"],
    "codex": AGENT_EMAILS["codex@openai.com"],
    "cursoragent": AGENT_EMAILS["cursoragent@cursor.com"],
    "copilot": ("GitHub", "github", "Copilot", False),
    "copilot-swe-agent[bot]": ("GitHub", "github", "Copilot", False),
    "devin-ai-integration[bot]": ("Cognition", "cognition", "Devin", False),
    "coderabbitai[bot]": ("CodeRabbit", "coderabbit", "CodeRabbit", False),
    "capy-ai[bot]": ("Capy", "capy", "Capy", False),
}
# Display casing for model words that Git names often write in lower case.
MODEL_WORDS = {word.casefold(): word for word in ("GPT", "ChatGPT", "GLM", "DeepSeek", "MiMo", "OpenCode", "MiniMax")}


def agent(email, login=None):
    """Return the agent behind a Git email or GitHub login, or None for a person."""
    email = (email or "").strip().casefold()
    noreply = re.fullmatch(r"(?:\d+\+)?([^@]+)@users\.noreply\.github\.com", email)
    login = (login or (noreply.group(1) if noreply else "")).casefold()
    return AGENT_EMAILS.get(email) or AGENT_LOGINS.get(login)


def human(login):
    """GitHub accounts credited as people: not AI agents and not other bot accounts."""
    return bool(login) and not login.casefold().endswith("[bot]") and agent(None, login) is None


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
          nodes { oid author { email user { login } } signature { state signer { login } }
            authors(first: 100) { nodes { email user { login } } pageInfo { hasNextPage } }
          }
          pageInfo { hasNextPage endCursor }
        }
      } } }
    }"""
    identities, authors, linked, seen_cursors = {}, {}, {}, set()
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
            actors = [*node["authors"]["nodes"], node["author"]]
            if any(actor.get("user") and (not isinstance(actor["user"].get("login"), str) or not actor["user"]["login"])
                   for actor in actors):
                raise ValueError(f"Invalid GitHub account identity on {sha}")
            # GitHub links agent trailers such as noreply@anthropic.com to accounts (claude, codex);
            # those commits are credited to models, so agents and bots never become people.
            people = [actor["user"]["login"] for actor in actors
                      if actor.get("user") and agent(actor.get("email"), actor["user"]["login"]) is None
                      and human(actor["user"]["login"])]
            primary = node["author"].get("user")
            signature = node.get("signature") or {}
            signer = (signature.get("signer") or {}).get("login")
            signed_by_author = (signature.get("state") == "VALID" and primary is not None
                                and signer is not None and signer.casefold() == primary["login"].casefold()
                                and primary["login"] in people)
            # Email-linked authors still receive credit, but only a matching signer
            # authenticates a commit's author for opt-in name/consent checks.
            identities[sha] = primary["login"] if signed_by_author else None
            linked[sha] = primary["login"] if primary and primary["login"] in people else None
            authors[sha] = {login.casefold(): login for login in people}
        if not page["pageInfo"]["hasNextPage"]:
            return identities, authors, linked
        cursor = page["pageInfo"]["endCursor"]
        if not cursor or cursor in seen_cursors:
            raise ValueError("GitHub history pagination did not advance")
        seen_cursors.add(cursor)


def pull_openers(root, repo, shas, reachable):
    """Map commits to the one account that opened the merged PRs containing them.

    A merge-commit or rebase merge leaves the contributor's own commits in history, so their
    SHAs never equal a PR's merge commit. GitHub's commit-to-PR association finds those PRs;
    only PRs merged into this history count, and commits whose PRs disagree stay unknown.
    """
    owner, name = repo.split("/")
    openers = {}
    shas = sorted(shas)
    # Small batches: commit-to-PR association is expensive, and large queries time out on GitHub.
    for start in range(0, len(shas), 20):
        batch = shas[start:start + 20]
        if any(not re.fullmatch(r"[0-9a-f]{40,64}", sha) for sha in batch):
            raise ValueError("Invalid commit identity in PR lookup")
        fields = " ".join(
            f'c{index}: object(oid: "{sha}") {{ ... on Commit {{ associatedPullRequests(first: 10) {{'
            " totalCount nodes { merged author { __typename login } mergeCommit { oid } } } } }"
            for index, sha in enumerate(batch))
        query = f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}"
        body = json.dumps(dict(query=query, variables=dict(owner=owner, name=name)))
        try:
            output = run(["gh", "api", "graphql", "--input", "-"], root, input_text=body)
        except RuntimeError:
            # GitHub reports transient query timeouts as failures; one retry keeps a release moving.
            output = run(["gh", "api", "graphql", "--input", "-"], root, input_text=body)
        data = json.loads(output)["data"]["repository"]
        for index, sha in enumerate(batch):
            pulls = ((data.get(f"c{index}") or {}).get("associatedPullRequests")) or {}
            nodes = pulls.get("nodes") or []
            if pulls.get("totalCount", 0) > len(nodes):
                continue
            # GraphQL names bot openers without "[bot]" (dependabot), so only User accounts count.
            logins = {(pull.get("author") or {}).get("login")
                      if (pull.get("author") or {}).get("__typename") == "User" else None for pull in nodes
                      if pull.get("merged") and (pull.get("mergeCommit") or {}).get("oid") in reachable}
            if len(logins) == 1 and None not in logins:
                openers[sha] = next(iter(logins))
    return openers


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
                "--format=%x00%H%x00%aI%x00%ae%x00%an%x00%(trailers:key=Co-authored-by,valueonly,separator=%x1f)%x00",
                head, "--"], root)
    fields = text.split("\0")
    if (len(fields) - 1) % 6:
        raise ValueError("Unexpected Git history format")
    commits = {}
    for index in range(1, len(fields), 6):
        sha, date, email, name, trailers, changes = fields[index:index + 6]
        # The Git author is read only to credit an agent that authored the commit; a person's
        # Git name or email is never recorded.
        trailers = trailers.split("\x1f")
        if agent(email):
            trailers.append(f"{name} <{email}>")
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
        commits[sha] = dict(date=utc_date(date), trailers=trailers, **counts)
    return commits


def model(trailer):
    """Recognize an agent's Git identity by its exact email, without inferring a model from humans."""
    match = re.fullmatch(r"\s*(.*?)\s*<([^<>]+)>\s*", trailer)
    if not match:
        return None
    name, email = match.groups()
    known = agent(email)
    if known is None:
        return None
    company, prefix, default, named = known

    def parse(text):
        """Split "GPT-6.1 Sol" into its model words and first version number."""
        words = [word for word in re.split(r"[\s_-]+", text) if word]
        # Drop a bare company prefix ("OpenAI Codex"), not a brand that names the model ("DeepSeek V3.2").
        if words and words[0].casefold() == company.casefold() != default.split()[0].casefold():
            words = words[1:]
        family, version = [], ""
        for word in words:
            if not version and re.fullmatch(r"[vV]?\d+(?:\.\d+)*", word):
                version = word.lstrip("vV")
            else:
                family.append(MODEL_WORDS.get(word.casefold(), word[:1].upper() + word[1:]))
        return family, version

    family, version = [], ""
    if named:
        # "Claude Opus 5.5 (1M context)", "GPT-6.1 Sol", "OpenAI Codex", "DeepSeek V4.1 Flash";
        # "Codex (GPT-6)" names its model in brackets when the outer name has no version.
        family, version = parse(re.sub(r"\([^)]*\)", " ", name))
        for inner in re.findall(r"\(([^)]*)\)", name):
            if not version and parse(inner)[1]:
                family, version = parse(inner)
    title = " ".join(family) or default
    return dict(id=f"{prefix}/" + title.casefold().replace(" ", "-") + (f"/{version}" if version else ""),
                company=company, model=title, version=version)


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


def verified_people(root, repo, people, identities, pull_authors, head="HEAD", linked=None):
    """Verify self-authored entries, a writer's PR carrying the person's own commit, or recorded
    consent committed by a repository writer."""
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
            # An integration PR that carries the person's own GitHub-linked commit, keeping its
            # authorship, is vouched for by the writer who opened it.
            carried = ((linked or {}).get(sha) or "").casefold() == login.casefold()
            if not human(actor) or not carried and (
                    not isinstance(entry.get("consent_source"), str) or not entry["consent_source"].strip()):
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
        run(["git", "cat-file", "-e", f"{head}:contributors/people.toml"], root)
    except RuntimeError as error:
        raise ValueError("Contributor preferences must match the release commit; "
                         f"commit or restore contributors/people.toml ({error})") from error
    commits = history(root, head)
    identities, authors, linked = commit_authors(root, repo, head)
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
    # Commits merged with a merge commit or rebase need GitHub's commit-to-PR association:
    # unlinked commits (credit) and the commits behind name preferences (consent).
    unknown = {sha for sha in commits if not authors[sha]}
    unknown |= {sha for shas in people_authors(root, head).values() for sha in shas if not identities.get(sha)}
    unknown -= pull_authors.keys()
    if unknown:
        pull_authors.update(pull_openers(root, repo, unknown, commits.keys()))
    people = load_people(root, head)
    verified = verified_people(root, repo, people, identities, pull_authors, head, linked)
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
        logins = authors[sha] or {login.casefold(): login for login in [pull_authors.get(sha)] if human(login)}
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
        if pull["sha"] in commits and pull["number"] not in seen_pulls and human(pull["login"]):
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

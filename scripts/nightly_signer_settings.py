#!/usr/bin/env python3
"""Settings for `refs/heads/nightly-signer` across the nightly apps. Read-only unless `apply`/`advance`/`rollback --apply`.

  plan     --repos all|a,b,...        read everything and print every write `apply` would make (needs admin:org)
  apply    --repos all|a,b,...        make them; per repo, skip what is already exact and refuse anything else
  readback --repos all|a,b,...        check every setting exactly
  advance  --repos <one> --sha S [--apply]   fast-forward one repo's branch to a reviewed main commit
  rollback --repos <one> [--apply]           drop the environment trust (verified), then cancel its signer runs
  --require-pinned-release                   also refuse release.yml callers not pinned to a full SHA (default: warn)

`all` means every app with targets in craft-common-actions main's nightly-apps.json. Two org rulesets, because a
ruleset's bypass covers all of its rules: the no-delete/no-force rule must live in a ruleset with no bypass at all.
A repo's branch is created at its main tip only once its switch-on PR has merged; the tip's callers, pins, policy
and trust closure are checked first. Nothing is ever forced, deleted or adopted. Needs PyYAML and the gh CLI.
"""
import argparse, base64, copy, json, re, subprocess, sys

import yaml  # PyYAML: workflows are checked as parsed jobs, not by regex over their text.

ORG, TEAM, BRANCH, ENV = "storytold", "release-managers", "nightly-signer", "release"
REF = f"refs/heads/{BRANCH}"
CCA = "storytold/craft-common-actions"
SIGNER_BRANCH_SUPPORT = "88148a57ef09ef407d73c0f385e04cc7f4c2720a"  # craft-common-actions #364 on main
RULESET_WRITERS = "nightly-signer: release-managers only"
RULESET_HISTORY = "nightly-signer: no force-push or deletion"
# Environment secret names each signer platform needs (PhotoCraft's 13 cover all three). Names only, never values.
SECRETS = {"linux": {"LINUX_SIGNING_KEY"},
           "macos": {"APPLE_CERTIFICATE", "APPLE_CERTIFICATE_PASSWORD", "APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID", "KEYCHAIN_PASSWORD"},
           "windows": {"AZURE_CERT_PROFILE", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_SIGNING_ACCOUNT",
                       "AZURE_SIGNING_ENDPOINT", "AZURE_TENANT_ID"}}
GUARD = "${{ !inputs.dry-run && github.ref == 'refs/heads/release' && 'release' || '' }}"
SHA40 = re.compile(r"[0-9a-f]{40}")
# The only reusable workflows an app may call: craft-common-actions' own, at a plain ref (local ./ calls are
# files in the same .github/workflows listing, so they are checked there).
CCA_CALL = re.compile(re.escape(CCA) + r"/\.github/workflows/([A-Za-z0-9_.-]+\.ya?ml)@([A-Za-z0-9._/-]+)")
# The tooling the signer pins and the pin checks verify; every other callee's call chain is resolved and checked.
PINNED_TOOLING = ("nightly-release.yml", "nightly-unsigned.yml")
MAX_CALL_DEPTH = 4  # GitHub allows ten levels; craft-common-actions nests at most two.
# --require-pinned-release: also require release.yml callers to pin a full SHA. Off by default while every app
# calls release.yml@main; its environment bindings are checked at the called ref either way.
OPTIONS = {"require_pinned_release": False}
MAX_FIRST_PARENT_STEPS = 500
MAX_CANCEL_ROUNDS = 5
ACTIVE_RUN_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")
# gh api arguments are fixed flags plus a method from this set and a REST path of plain characters: nothing
# read from a repository can become an option or a second argument.
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
API_PATH = re.compile(r"(?:repos|orgs)/[A-Za-z0-9_.~%/?=&,-]{1,400}")


class Refuse(Exception):
    pass


def api_argument(method, path):
    """The method and path, refused unless they are a known method and a plain REST path (no option, no ..)."""
    if method not in METHODS:
        raise Refuse(f"unsupported API method {method!r}")
    if not API_PATH.fullmatch(path or "") or ".." in path.split("?", 1)[0].split("/"):
        raise Refuse(f"refusing an unexpected API path {path!r}")
    return METHODS[METHODS.index(method)], path


def gh(method, path, body=None, write=False, apply=False, ok404=False):
    method, path = api_argument(method, path)
    shown = f"{method} {path}" + (f"  {json.dumps(body, sort_keys=True)}" if body is not None else "")
    if write and not apply:
        print("  WOULD ", shown); return None
    print("  WRITE " if write else "  READ  ", shown)
    run = subprocess.run(["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
                         + (["--input", "-"] if body is not None else []),
                         input=json.dumps(body) if body is not None else None, capture_output=True, text=True)
    if run.returncode:
        if ok404 and "HTTP 404" in run.stderr:
            return None
        raise Refuse(f"failed: {shown}: {run.stderr.strip().splitlines()[-1] if run.stderr.strip() else 'error'}")
    return json.loads(run.stdout) if run.stdout.strip() else None


def text_at(repo, path, ref, ok404=False):
    body = gh("GET", f"repos/{repo}/contents/{path}?ref={ref}", ok404=ok404)
    if body is None:
        return None
    # The contents API returns encoding "none" and no content for a file over 1 MB: that must not read as empty.
    if not isinstance(body, dict) or body.get("encoding") != "base64" or not isinstance(body.get("content"), str):
        raise Refuse(f"{repo} {path} at {ref} did not come back as a base64 file (too large, or not a file)")
    return base64.b64decode(body["content"]).decode("utf-8")


def ancestor(repo, older, newer):
    data = gh("GET", f"repos/{repo}/compare/{older}...{newer}?per_page=1&page=2")
    return data.get("status") in ("ahead", "identical") and data.get("behind_by") == 0


def registry():
    return json.loads(text_at(CCA, "nightly-apps.json", "main"))


def registry_apps(data=None):
    data = registry() if data is None else data
    return sorted(app for app, row in data["apps"].items() if row["targets"])


def required_secrets(row):
    return set().union(*(SECRETS[t["target"]["os"]] for t in row["targets"]))


def check_secrets(app, row):
    """The release environment must hold every signing secret its targets need; repo-level copies don't count."""
    names = lambda path: {s["name"] for s in gh("GET", path)["secrets"]}
    have = names(f"repos/{ORG}/{app}/environments/{ENV}/secrets?per_page=100")
    missing = sorted(required_secrets(row) - have)
    if missing:
        repo_level = sorted(set(missing) & names(f"repos/{ORG}/{app}/actions/secrets?per_page=100"))
        detail = f" ({', '.join(repo_level)} exists only as a repo-level secret; the signer reads the {ENV} environment)" if repo_level else ""
        raise Refuse(f"{app} {ENV} environment lacks signing secrets {missing}{detail}")
    print(f"  ok     {app} {ENV} environment holds all {len(required_secrets(row))} signing secret names its targets need")


def main_tip(app):
    return gh("GET", f"repos/{ORG}/{app}/branches/main")["commit"]["sha"]


class StrictLoader(yaml.BaseLoader):
    """BaseLoader (every scalar a string) that refuses a duplicate mapping key: GitHub rejects those files, and
    BaseLoader alone would keep the last value and check something GitHub never runs."""


def _unique_mapping(loader, node):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate or non-string key {key!r}", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def parse_workflow(text, where):
    """A workflow's jobs as parsed mappings (string scalars only), or a refusal. Every key the checks read has
    the type GitHub requires, so a check never meets a list where it expects a string."""
    try:
        doc = yaml.load(text, Loader=StrictLoader)
    except yaml.YAMLError as error:
        raise Refuse(f"{where} is not valid YAML: {str(error).splitlines()[0]}")
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict) or not all(isinstance(job, dict) for job in jobs.values()):
        raise Refuse(f"{where} has no mapping of jobs")
    for name, job in jobs.items():
        env = job.get("environment")
        if any(key in job and not isinstance(job[key], str) for key in ("uses", "if")) or \
                ("with" in job and not isinstance(job["with"], dict)) or \
                (env is not None and not isinstance(env, str) and not (isinstance(env, dict) and isinstance(env.get("name"), str))):
            raise Refuse(f"{where} job {name}: uses and if must be strings, with a mapping, environment a name")
    return jobs


def environment_name(job):
    env = job.get("environment")
    return env.get("name") if isinstance(env, dict) else env


def callee_problems(callee, ref, seen, depth=1):
    """Every job a craft-common-actions callee runs, down its whole call chain: an environment only behind the
    stable-only guard, and no call outside craft-common-actions."""
    where = f"{CCA} {callee}@{ref}"
    if (callee, ref) in seen:
        return []
    seen.add((callee, ref))
    if depth > MAX_CALL_DEPTH:
        return [f"{where}: reusable call chain deeper than {MAX_CALL_DEPTH}"]
    problems = []
    for name, job in parse_workflow(text_at(CCA, f".github/workflows/{callee}", ref), where).items():
        env = environment_name(job)
        if env is not None and env != GUARD:
            problems.append(f"{where} job {name} binds environment {env!r} without the stable-only guard")
        uses = job.get("uses")
        if uses is None:
            continue
        local = re.fullmatch(r"\./\.github/workflows/([A-Za-z0-9_.-]+\.ya?ml)", uses)
        nested = (local.group(1), ref) if local else (CCA_CALL.fullmatch(uses).groups() if CCA_CALL.fullmatch(uses) else None)
        if nested is None:
            problems.append(f"{where} job {name} calls {uses!r}, outside {CCA}")
        else:
            problems += callee_problems(*nested, seen, depth + 1)
    return problems


def signer_job_problems(app, jobs, pin):
    """nightly-sign.yml must be one job that calls the pinned signer for this app, guarded to the signer branch."""
    signer = {name: job for name, job in jobs.items()
              if (CCA_CALL.fullmatch(job.get("uses") or "") or [None])[0] is not None
              and CCA_CALL.fullmatch(job["uses"]).group(1) == "nightly-release.yml"}
    if len(signer) != 1:
        return [f"nightly-sign.yml has {len(signer)} jobs calling nightly-release.yml, expected exactly one"]
    name, job = next(iter(signer.items()))
    problems, condition = [], " ".join(str(job.get("if", "")).split())
    if condition.startswith("false") or "false &&" in condition:
        problems.append("nightly-sign.yml is still switched off (`false &&`)")
    if not all(guard in condition for guard in (f"github.repository == '{ORG}/{app}'", f"github.ref == '{REF}'",
                                                 "github.ref_type == 'branch'")):
        problems.append(f"nightly-sign.yml does not guard on {ORG}/{app}, {REF} and a branch ref")
    inputs = job.get("with") if isinstance(job.get("with"), dict) else {}
    if inputs.get("app-name") != app:
        problems.append(f"nightly-sign.yml does not pass app-name: {app}")
    if CCA_CALL.fullmatch(job["uses"]).group(2) != pin or inputs.get("tooling-revision") != pin:
        problems.append(f"nightly-sign.yml job {name} does not call nightly-release.yml and pass tooling-revision at {pin}")
    problems += forwarded_secrets_problems(app, job, name)
    return problems


def forwarded_secrets_problems(app, job, name):
    """A caller may forward the reviewable signing secrets, an identity mapping of exactly the names its targets need.

    The callee cannot read the release environment (an environment does not reach a cross-repo reusable
    workflow), so the caller forwards them. Anything else stays refused: `inherit`, a value taken from a
    different secret, an extra or missing name, and an environment on the calling job.
    """
    wanted = required_secrets(registry()["apps"][app])
    if "environment" in job:
        return [f"nightly-sign.yml job {name} binds an environment; the caller forwards the {ENV} secrets by name instead"]
    if "secrets" not in job:
        return [f"nightly-sign.yml job {name} forwards no secrets; the callee cannot read the {ENV} environment"]
    spec = job["secrets"]
    if not isinstance(spec, dict):
        return [f"nightly-sign.yml job {name} passes secrets: {spec!r}; only an identity mapping of the reviewed names is allowed"]
    problems = []
    for key, value in spec.items():
        if value != "${{ secrets." + key + " }}":
            problems.append(f"nightly-sign.yml job {name} forwards {key} from {value!r}, not from secrets.{key}")
    extra, missing = sorted(set(spec) - wanted), sorted(wanted - set(spec))
    if extra:
        problems.append(f"nightly-sign.yml job {name} forwards secrets outside the reviewed set: {extra}")
    if missing:
        problems.append(f"nightly-sign.yml job {name} does not forward {missing}, which this app's targets need")
    return problems


def signing_policy(app, sha, text):
    """The decoded signing policy, or a refusal naming the app and file (valid JSON can still be the wrong shape)."""
    where = f"{app} .github/nightly-signing-policy.json at {sha[:10]}"
    try:
        policy = json.loads(text)
    except ValueError as error:
        raise Refuse(f"{where} is not JSON: {error}")
    fields = ("release_branch", "producer_tooling_revision", "signer_tooling_revision")
    if not isinstance(policy, dict) or not all(isinstance(policy.get(field), str) for field in fields):
        raise Refuse(f"{where} is not an object with string {', '.join(fields)}")
    return policy


def check_tree(app, sha):
    """Refuse a tip whose switch-on is incomplete or where anything else can reach an environment."""
    repo, problems = f"{ORG}/{app}", []
    sign = text_at(repo, ".github/workflows/nightly-sign.yml", sha, ok404=True)
    policy_text = text_at(repo, ".github/nightly-signing-policy.json", sha, ok404=True)
    if sign is None or policy_text is None:
        raise Refuse("switch-on not merged: nightly-sign.yml or .github/nightly-signing-policy.json missing at main")
    policy = signing_policy(app, sha, policy_text)
    if policy["release_branch"] != BRANCH:
        problems.append(f"policy release_branch is {policy['release_branch']!r}, not {BRANCH!r}")
    pins, seen = {policy["producer_tooling_revision"], policy["signer_tooling_revision"]}, set()
    sign_jobs = None
    for item in gh("GET", f"repos/{repo}/contents/.github/workflows?ref={sha}"):
        if not item["name"].endswith((".yml", ".yaml")):
            continue
        jobs = parse_workflow(text_at(repo, item["path"], sha), item["name"])
        if item["name"] == "nightly-sign.yml":
            sign_jobs = jobs
        for name, job in jobs.items():
            if "environment" in job:
                problems.append(f"{item['name']} binds an environment directly")
            uses = job.get("uses")
            if uses is None or re.fullmatch(r"\./\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", uses):
                continue  # A local callee is a file of this same listing, checked in its own turn.
            call = CCA_CALL.fullmatch(uses)
            if call is None:
                problems.append(f"{item['name']} job {name} calls {uses!r}, which is not an approved {CCA} workflow")
                continue
            callee, pin = call.groups()
            if callee == "nightly-release.yml" and item["name"] != "nightly-sign.yml":
                problems.append(f"{item['name']} calls nightly-release.yml; only nightly-sign.yml may")
            if callee in PINNED_TOOLING:
                pins.add(pin)
                if not SHA40.fullmatch(pin):
                    problems.append(f"{item['name']} calls {callee}@{pin}, not a full SHA")
            else:
                if callee == "release.yml" and not SHA40.fullmatch(pin):
                    if OPTIONS["require_pinned_release"]:
                        problems.append(f"{item['name']} calls release.yml@{pin}, not a full SHA (--require-pinned-release)")
                    else:
                        print(f"  WARN   {app} {item['name']} calls release.yml@{pin}, not a full SHA: its environment guard is "
                              f"checked at today's {pin} only (pass --require-pinned-release to refuse)")
                problems += callee_problems(callee, pin, seen)
    if len(pins) != 1:
        problems.append(f"caller and policy pins differ: {sorted(map(str, pins))}")
    else:
        pin = next(iter(pins))
        problems += signer_job_problems(app, sign_jobs or {}, pin)
        if not (SHA40.fullmatch(pin) and ancestor(CCA, pin, "main") and ancestor(CCA, SIGNER_BRANCH_SUPPORT, pin)):
            problems.append(f"pin {pin} is not a craft-common-actions main commit that includes #364")
    if problems:
        raise Refuse("trust-closure check refused at " + sha[:10] + ":\n      " + "\n      ".join(problems))
    print(f"  ok     {app} main {sha[:10]}: switched on, pinned {next(iter(pins))[:10]}, signer branch {BRANCH}, trust closure clean")


def canonical(apps, team):
    target = {"ref_name": {"include": [REF], "exclude": []}, "repository_name": {"include": sorted(apps), "exclude": [], "protected": True}}
    writers = {"name": RULESET_WRITERS, "target": "branch", "enforcement": "active", "conditions": target,
               "rules": [{"type": "creation"}, {"type": "update", "parameters": {"update_allows_fetch_and_merge": False}}],
               "bypass_actors": [{"actor_type": "Team", "actor_id": team, "bypass_mode": "always"},
                                 {"actor_type": "OrganizationAdmin", "actor_id": None, "bypass_mode": "always"}]}
    history = {"name": RULESET_HISTORY, "target": "branch", "enforcement": "active", "conditions": json.loads(json.dumps(target)),
               "rules": [{"type": "non_fast_forward"}, {"type": "deletion"}], "bypass_actors": []}
    return writers, history


def org_rulesets():
    found = gh("GET", f"orgs/{ORG}/rulesets?per_page=100&includes_parents=false")
    if len(found) >= 100:
        raise Refuse("100 or more org rulesets: paginate before trusting this read")
    return {item["name"]: gh("GET", f"orgs/{ORG}/rulesets/{item['id']}") for item in found if item["name"] in (RULESET_WRITERS, RULESET_HISTORY)}


def shape(ruleset):
    """Everything but the covered repo names. Another repository selector (repository_id, a property) stays in the
    conditions, so it differs from the canonical shape instead of failing to read."""
    bypass = sorted((b.get("actor_type"), b.get("actor_id") if b.get("actor_type") == "Team" else None, b.get("bypass_mode"))
                    for b in ruleset.get("bypass_actors") or [])
    conditions = copy.deepcopy(ruleset.get("conditions") or {})
    if isinstance(conditions.get("repository_name"), dict):
        conditions["repository_name"].pop("include", None)
    rules = sorted(json.dumps({"type": r.get("type"), **({"parameters": r["parameters"]} if r.get("parameters") else {})}, sort_keys=True)
                   for r in ruleset.get("rules") or [])
    return {"target": ruleset.get("target"), "enforcement": ruleset.get("enforcement"), "conditions": conditions, "rules": rules, "bypass": bypass}


def covered(ruleset):
    """The repo names a canonical-shaped ruleset covers (read only after shape() matched)."""
    return set(ruleset["conditions"]["repository_name"]["include"])


def env_trust(app):
    env = gh("GET", f"repos/{ORG}/{app}/environments/{ENV}", ok404=True)
    if env is None:
        raise Refuse(f"{app} has no {ENV} environment")
    mode = env.get("deployment_branch_policy") or {}
    if mode != {"protected_branches": False, "custom_branch_policies": True}:
        raise Refuse(f"{app} {ENV} environment branch mode is {mode}, expected custom policies only")
    policies = gh("GET", f"repos/{ORG}/{app}/environments/{ENV}/deployment-branch-policies?per_page=100")["branch_policies"]
    return sorted((p["name"], p.get("type", "branch")) for p in policies)


def ensure_rulesets(apps, apply):
    """Create or widen the two rulesets. Returns which repos each covered before this run, or None in a plan
    whose token cannot read the org rulesets (then no ruleset change is proposed: it is undetermined)."""
    team = gh("GET", f"orgs/{ORG}/teams/{TEAM}")["id"]
    try:
        current = org_rulesets()
    except Refuse as error:
        if apply:
            raise
        # A plan with a token lacking admin:org still checks every repo; apply always needs the real read.
        print(f"  NOTE   org ruleset changes undetermined: cannot read the org rulesets ({error}); none proposed")
        return None
    before = {}
    for body in canonical(apps, team):
        have = current.get(body["name"])
        if have is None:
            gh("POST", f"orgs/{ORG}/rulesets", body, write=True, apply=apply); continue
        if shape(have) != shape(body):
            raise Refuse(f"org ruleset {body['name']!r} differs from the canonical one; reconcile by hand, never overwrite")
        before[body["name"]] = covered(have)
        missing = sorted(set(apps) - before[body["name"]])
        if not missing:
            print(f"  exact  org ruleset {body['name']!r} already covers {len(apps)} repos"); continue
        update = {k: copy.deepcopy(have[k]) for k in ("name", "target", "enforcement", "conditions", "rules", "bypass_actors")}
        wanted = before[body["name"]] | set(apps)
        update["conditions"]["repository_name"]["include"] = sorted(wanted)  # Only the repo list changes.
        if apply:
            # GitHub has no conditional ruleset update. Re-read just before the PUT and refuse a list another run
            # changed, so this PUT never drops its additions; read again after, so a concurrent PUT that dropped
            # ours is caught before any trust is granted.
            if covered(gh("GET", f"orgs/{ORG}/rulesets/{have['id']}")) != before[body["name"]]:
                raise Refuse(f"org ruleset {body['name']!r} changed since this run read it; is another apply running? Re-run alone")
        gh("PUT", f"orgs/{ORG}/rulesets/{have['id']}", update, write=True, apply=apply)
        if apply and not wanted <= covered(gh("GET", f"orgs/{ORG}/rulesets/{have['id']}")):
            raise Refuse(f"org ruleset {body['name']!r} lost repos right after this run's update; is another apply running? Re-run alone")
    return before


def live_coverage():
    """The repos both canonical rulesets cover right now (an empty set if either is missing or differs)."""
    team, current = gh("GET", f"orgs/{ORG}/teams/{TEAM}")["id"], org_rulesets()
    sets = []
    for body in canonical([], team):
        have = current.get(body["name"])
        sets.append(covered(have) if have is not None and shape(have) == shape(body) else set())
    return sets[0] & sets[1]


def uncovered_trust(apps):
    """Apps whose release environment trusts the signer branch although the live rulesets don't cover them."""
    live, found = live_coverage(), []
    for app in sorted(set(apps) - live):
        policies = gh("GET", f"repos/{ORG}/{app}/environments/{ENV}/deployment-branch-policies?per_page=100", ok404=True) or {}
        if any(p.get("name") == BRANCH for p in policies.get("branch_policies") or []):
            found.append(app)
    return found


def owned_before(app, before):
    """True only if both rulesets already covered this repo when the run started: then a branch there was made by
    an earlier apply, under the rulesets. None when the rulesets could not be read (plan only)."""
    if before is None:
        return None
    return all(app in before.get(name, set()) for name in (RULESET_WRITERS, RULESET_HISTORY))


def setup_repo(app, apply, owned, row):
    """Per repo: secrets, branch at the switched-on main tip, then environment trust. Skip exact, refuse anything else."""
    trust = env_trust(app)
    check_secrets(app, row)
    if trust not in ([("release", "branch")], [("nightly-signer", "branch"), ("release", "branch")]):
        raise Refuse(f"{app} {ENV} environment trusts {trust}; expected only release")
    branch = gh("GET", f"repos/{ORG}/{app}/git/ref/heads/{BRANCH}", ok404=True)
    if branch is None:
        tip = main_tip(app)
        check_tree(app, tip)
        gh("POST", f"repos/{ORG}/{app}/git/refs", {"ref": REF, "sha": tip}, write=True, apply=apply)
    else:
        head = branch["object"]["sha"]
        # Resume only a branch our own earlier apply made: both rulesets already covered this repo when the run
        # started (so only release-managers could have created it), and it is a checked main commit.
        if owned is None:
            raise Refuse(f"{REF} already exists at {head[:10]}; whether an earlier apply made it is undetermined without the org rulesets")
        if not owned or not ancestor(f"{ORG}/{app}", head, main_tip(app)):
            raise Refuse(f"{REF} already exists at {head[:10]} and is not ours: it may be hostile. Delete it; never adopt it.")
        check_tree(app, head)
        print(f"  exact  {app} {REF} at {head[:10]}")
    # Trust (new or kept) only for a repo the live rulesets cover now, read after every ruleset write of this run.
    if apply and app not in live_coverage():
        raise Refuse(f"the org rulesets do not cover {app} now; not granting or keeping {BRANCH} trust (run rollback if it is trusted)")
    if ("nightly-signer", "branch") in trust:
        print(f"  exact  {app} {ENV} environment already trusts {BRANCH}")
    else:
        gh("POST", f"repos/{ORG}/{app}/environments/{ENV}/deployment-branch-policies", {"name": BRANCH, "type": "branch"}, write=True, apply=apply)


def readback(apps, registry_rows):
    team = gh("GET", f"orgs/{ORG}/teams/{TEAM}")["id"]
    problems, current = [], org_rulesets()
    for body in canonical(apps, team):
        have = current.get(body["name"])
        if have is None:
            problems.append(f"missing org ruleset {body['name']!r}"); continue
        if shape(have) != shape(body):
            problems.append(f"{body['name']!r} differs: {shape(have)}"); continue
        missing = sorted(set(apps) - covered(have))
        if missing:
            problems.append(f"{body['name']!r} does not cover {missing}")
    for app in apps:
        try:
            trust = env_trust(app)
            check_secrets(app, registry_rows[app])
            if trust != [("nightly-signer", "branch"), ("release", "branch")]:
                problems.append(f"{app}: {ENV} environment trusts {trust}")
            branch = gh("GET", f"repos/{ORG}/{app}/git/ref/heads/{BRANCH}", ok404=True)
            if branch is None:
                problems.append(f"{app}: no {REF}")
            elif not ancestor(f"{ORG}/{app}", branch["object"]["sha"], main_tip(app)):
                problems.append(f"{app}: {REF} at {branch['object']['sha'][:10]} is not on main")
            else:
                print(f"  ok     {app} {REF} at {branch['object']['sha'][:10]}")
        except Refuse as error:
            problems.append(f"{app}: {error}")
    print("\n".join(problems) or f"all settings read back exactly as planned for {len(apps)} repos")
    return 1 if problems else 0


def queued_signer_runs(app, max_pages=20):
    """Every active signer run on the branch: each active status asked for separately, every page, deduplicated."""
    found = set()
    for status in ACTIVE_RUN_STATUSES:
        for page in range(1, max_pages + 1):
            data = gh("GET", f"repos/{ORG}/{app}/actions/workflows/nightly-sign.yml/runs?branch={BRANCH}&status={status}"
                             f"&per_page=100&page={page}", ok404=True) or {"workflow_runs": []}
            runs = data.get("workflow_runs") or []
            found |= {r["id"] for r in runs if r.get("status") in ACTIVE_RUN_STATUSES}
            if len(runs) < 100:
                break
        else:
            raise Refuse(f"more than {max_pages * 100} {status} signer runs: refusing to guess which are active")
    return sorted(found)


def first_parent_of_main(app, sha, head):
    """True if sha is on main's first-parent line above head: a commit that was main's tip, not one from inside a
    merged branch. Walks first parents down from main's tip; refuses rather than guessing past a bound."""
    repo, commit = f"{ORG}/{app}", main_tip(app)
    for _ in range(MAX_FIRST_PARENT_STEPS):
        if commit == sha:
            return True
        if commit == head:
            return False
        parents = gh("GET", f"repos/{repo}/commits/{commit}")["parents"]
        if not parents:
            return False
        commit = parents[0]["sha"]
    raise Refuse(f"{sha[:10]} is more than {MAX_FIRST_PARENT_STEPS} first-parent commits below main; advance in smaller steps")


def advance(app, sha, apply):
    head = gh("GET", f"repos/{ORG}/{app}/git/ref/heads/{BRANCH}")["object"]["sha"]
    if not (ancestor(f"{ORG}/{app}", sha, main_tip(app)) and ancestor(f"{ORG}/{app}", head, sha) and head != sha):
        raise Refuse(f"{sha} is not a main commit that fast-forwards {REF} from {head}")
    if not first_parent_of_main(app, sha, head):
        raise Refuse(f"{sha} is not on main's first-parent history (a commit from inside a merged branch, never main's tip)")
    if queued_signer_runs(app):
        raise Refuse("a nightly-sign run is queued or running; it would fail closed after the move. Wait for it.")
    check_tree(app, sha)
    print(f"  review first: https://github.com/{ORG}/{app}/compare/{head}...{sha} (all of .github/ and packaging/)")
    gh("PATCH", f"repos/{ORG}/{app}/git/refs/heads/{BRANCH}", {"sha": sha, "force": False}, write=True, apply=apply)


def cancel_round(app, apply, failures):
    """Ask once to cancel every active signer run; returns how many were active."""
    try:
        runs = queued_signer_runs(app)
    except Refuse as error:
        failures.append(f"listing signer runs: {error}"); return 0
    for run_id in runs:  # Running jobs keep secrets they already read.
        try:
            gh("POST", f"repos/{ORG}/{app}/actions/runs/{run_id}/force-cancel", write=True, apply=apply)
        except Refuse as error:
            if "HTTP 409" in str(error):
                print(f"  note   run {run_id} could not be cancelled because it is no longer running (HTTP 409)")
            else:
                failures.append(f"cancelling run {run_id}: {error}")
    return len(runs)


def rollback(app, apply, wait=None):
    """Revoke the environment trust first and verify it, so no run can enter the environment from here on. Then
    cancel the active signer runs, each on its own, and list again until none is left (force-cancel is
    asynchronous). Every failure is reported. The branch and rulesets stay on purpose: the security-relevant state
    is the environment trusting release only, and deleting the branch needs the no-bypass history ruleset off."""
    import time
    wait = time.sleep if wait is None else wait
    failures = []
    try:
        for policy in gh("GET", f"repos/{ORG}/{app}/environments/{ENV}/deployment-branch-policies?per_page=100")["branch_policies"]:
            if policy["name"] == BRANCH:
                gh("DELETE", f"repos/{ORG}/{app}/environments/{ENV}/deployment-branch-policies/{policy['id']}", write=True, apply=apply)
        if apply and env_trust(app) != [("release", "branch")]:
            failures.append(f"{ENV} environment still trusts more than release")
    except Refuse as error:
        failures.append(f"revoking the {BRANCH} trust: {error}")
    active = cancel_round(app, apply, failures)
    rounds = 1
    while apply and active and rounds < MAX_CANCEL_ROUNDS:
        wait(10)
        active, rounds = cancel_round(app, apply, failures), rounds + 1
    if apply and active:
        # The last round's count was read before its cancellations: read once more before calling it a failure.
        wait(10)
        try:
            active = len(queued_signer_runs(app))
        except Refuse as error:
            failures.append(f"listing signer runs: {error}")
        if active:
            failures.append(f"{active} signer runs still active after {MAX_CANCEL_ROUNDS} cancellation rounds")
    print("  Keep the rulesets until the environment is verified; if compromise is suspected, rotate the release secrets.")
    if failures:
        raise Refuse("; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("plan", "apply", "readback", "advance", "rollback"))
    parser.add_argument("--repos", required=True, help="all, or a comma-separated list of app names")
    parser.add_argument("--sha"); parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require-pinned-release", action="store_true",
                        help="also refuse release.yml callers that do not pin a full SHA (every app calls @main today)")
    args = parser.parse_args()
    OPTIONS["require_pinned_release"] = args.require_pinned_release
    reg = registry()
    admitted = registry_apps(reg)
    # Every value that reaches a gh argument comes from the registry or is rebuilt from a parsed number: the
    # command line only selects among the admitted names, never supplies one.
    requested = set(admitted) if args.repos == "all" else set(args.repos.split(","))
    unknown = sorted(requested - set(admitted))
    if unknown:
        sys.exit(f"not admitted in craft-common-actions main nightly-apps.json: {unknown}")
    apps = [app for app in admitted if app in requested and re.fullmatch(r"[a-z0-9][a-z0-9-]*", app)]
    if args.mode in ("advance", "rollback"):
        if len(apps) != 1:
            sys.exit(f"{args.mode} takes exactly one repo")
        if args.mode == "advance" and not re.fullmatch(r"[0-9a-f]{40}", args.sha or ""):
            sys.exit("advance needs --sha <full reviewed main commit>")
        sha = format(int(args.sha, 16), "040x") if args.mode == "advance" else None
        try:
            advance(apps[0], sha, args.apply) if args.mode == "advance" else rollback(apps[0], args.apply)
        except Refuse as error:
            sys.exit(f"refused: {error}")
        return
    if args.mode == "readback":
        sys.exit(readback(apps, reg["apps"]))
    apply = args.mode == "apply"
    print(f"== org rulesets for {REF} over {len(apps)} repos")
    try:
        existed = ensure_rulesets(apps, apply)
    except Refuse as error:
        sys.exit(f"refused: {error}")
    ready, blocked = [], []
    for app in apps:
        print(f"== {app}")
        try:
            # Rulesets exist (or are created above) before any branch, so a new branch is protected from its first moment.
            setup_repo(app, apply, owned_before(app, existed), reg["apps"][app])
            ready.append(app)
        except Refuse as error:
            print(f"  REFUSED {app}: {error}"); blocked.append(app)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            # An unexpected response shape refuses this repo, never the remaining ones.
            print(f"  REFUSED {app}: unexpected GitHub response ({type(error).__name__}: {error})"); blocked.append(app)
    print(f"== {'applied' if apply else 'would apply'}: {', '.join(ready) or 'none'}; refused: {', '.join(blocked) or 'none'}")
    # A failed readback after apply is a failed apply.
    verified = readback(ready, reg["apps"]) if apply and ready else 0
    if apply:
        # Across every admitted repo, not just this run's: trust must never outlive ruleset coverage.
        exposed = uncovered_trust(admitted)
        if exposed:
            print(f"  EXPOSED {', '.join(exposed)}: {ENV} trusts {BRANCH} without the rulesets; run rollback for each now")
            verified = 1
    sys.exit(1 if blocked or verified else 0)


if __name__ == "__main__":
    main()

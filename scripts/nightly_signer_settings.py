#!/usr/bin/env python3
"""Settings for `refs/heads/nightly-signer` across the nightly apps. Read-only unless `apply` or `advance`/`enable`/`rollback --apply`.

  plan     --repos all|a,b,...        read everything and print every write `apply` would make (needs admin:org)
  apply    --repos all|a,b,...        make them; per repo, skip what is already exact and refuse anything else
  readback --repos all|a,b,...        check every setting exactly
  advance  --repos <one> --sha S [--apply]   fast-forward one repo's branch to a reviewed main commit
  enable   --repos <one> --sha S [--apply]   explicitly re-enable only the fully checked, reviewed branch head
  rollback --repos <one> [--apply]           revoke environment trust, disable workflow and cancel signer runs
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
SIGNING_NAMES = set().union(*SECRETS.values())
SIGNING_JOBS = {"sign-windows", "sign-macos", "sign-linux"}
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
    try:
        run = subprocess.run(["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
                             + (["--input", "-"] if body is not None else []),
                             input=json.dumps(body) if body is not None else None, capture_output=True, text=True)
    except (OSError, UnicodeError) as error:
        raise Refuse(f"failed: {method} {path}: could not read GitHub CLI result ({type(error).__name__})") from error
    if run.returncode:
        if ok404 and "HTTP 404" in run.stderr:
            return None
        raise Refuse(f"failed: {shown}: {run.stderr.strip().splitlines()[-1] if run.stderr.strip() else 'error'}")
    try:
        return json.loads(run.stdout) if run.stdout.strip() else None
    except json.JSONDecodeError as error:
        raise Refuse(f"failed: {method} {path}: invalid JSON from GitHub CLI") from error


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


def registry(ref="main"):
    return json.loads(text_at(CCA, "nightly-apps.json", ref))


def registry_apps(data=None):
    data = registry() if data is None else data
    return sorted(app for app, row in data["apps"].items() if row["targets"])


def required_secrets(row):
    targets = row.get("targets") if isinstance(row, dict) else None
    if not isinstance(targets, list) or not targets or any(
            not isinstance(item, dict) or not isinstance(item.get("target"), dict)
            or not isinstance(item["target"].get("os"), str) or item["target"]["os"] not in SECRETS
            for item in targets):
        raise Refuse("UNVERIFIED signing targets in the reviewed registry row")
    return set().union(*(SECRETS[t["target"]["os"]] for t in targets))


def metadata_list(path, key):
    """Read a bounded, complete metadata inventory; missing counts or changing pages are not evidence."""
    found, total = [], None
    for page in range(1, 21):
        data = gh("GET", f"{path}?per_page=100&page={page}")
        if not isinstance(data, dict) or not isinstance(data.get(key), list) or type(data.get("total_count")) is not int:
            raise Refuse(f"UNVERIFIED metadata inventory: {path}")
        batch, count = data[key], data["total_count"]
        total = count if total is None else total
        if count != total or not 0 <= total <= 2000 or len(batch) > 100:
            raise Refuse(f"UNVERIFIED changing or unbounded metadata: {path}")
        found.extend(batch)
        if len(found) > total:
            raise Refuse(f"UNVERIFIED metadata count: {path}")
        if len(found) == total:
            return found
        if len(batch) < 100:
            raise Refuse(f"UNVERIFIED incomplete metadata: {path}")
    raise Refuse(f"UNVERIFIED metadata pagination exhausted: {path}")


def secret_names(path):
    items = metadata_list(path, "secrets")
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in items):
        raise Refuse(f"UNVERIFIED secret-name metadata: {path}")
    names = [item["name"] for item in items]
    if len(set(names)) != len(names):
        raise Refuse(f"UNVERIFIED duplicate secret-name metadata: {path}")
    return set(names)


def check_secrets(app, row):
    """Read names only: signing credentials belong exclusively to the release environment.

    The target-specific identity map is a reusable-workflow interface, not permission to create caller-scope
    copies. Environment secrets resolve on the callee's signing jobs. Org metadata is the repository's
    effective inventory (including selected/private visibility), not every secret in the organization.
    """
    repo = f"repos/{ORG}/{app}"
    try:
        environment = secret_names(f"{repo}/environments/{ENV}/secrets")
        repository = secret_names(f"{repo}/actions/secrets")
        organization = secret_names(f"{repo}/actions/organization-secrets")
    except Refuse as error:
        raise Refuse(f"{app} signing-secret metadata UNVERIFIED: {error}")
    copies = sorted(SIGNING_NAMES & (repository | organization))
    if copies:
        raise Refuse(f"{app} signing credential names at repository or effective organization scope: {copies}; "
                     f"only the {ENV} environment may hold them")
    missing = sorted(required_secrets(row) - environment)
    if missing:
        raise Refuse(f"{app} {ENV} environment is missing signing secret names: {missing}")
    print(f"  ok     {app} signing credential names verified at {ENV} environment scope only (values never read)")


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


def callee_problems(callee, ref, seen, depth=1, nightly=False, signer=False):
    """Resolve the whole chain, allowing release credentials only on the pinned native signer jobs.

    The stable release chain retains its existing stable-only guard; nightly producers and nested callees
    have no environment admission, even with that guard. Context is part of the visited key.
    """
    where = f"{CCA} {callee}@{ref}"
    key = (callee, ref, nightly, signer)
    if key in seen:
        return []
    if depth > MAX_CALL_DEPTH:
        return [f"{where}: reusable call chain deeper than {MAX_CALL_DEPTH}"]
    seen.add(key)
    problems = []
    jobs = parse_workflow(text_at(CCA, f".github/workflows/{callee}", ref), where)
    if signer:
        missing = sorted(SIGNING_JOBS - set(jobs))
        if missing:
            problems.append(f"{where}: missing native signing jobs {missing}")
    for name, job in jobs.items():
        env = environment_name(job)
        if signer and name in SIGNING_JOBS:
            if env != ENV or "uses" in job:
                problems.append(f"{where} job {name} must bind environment {ENV!r} on a native signing job")
        elif env is not None and (nightly or env != GUARD):
            problems.append(f"{where} job {name} binds environment {env!r} without the stable-only guard"
                            if not nightly else f"{where} job {name} binds an environment outside native signing jobs")
        uses = job.get("uses")
        if uses is None:
            continue
        local = re.fullmatch(r"\./\.github/workflows/([A-Za-z0-9_.-]+\.ya?ml)", uses)
        nested = (local.group(1), ref) if local else (CCA_CALL.fullmatch(uses).groups() if CCA_CALL.fullmatch(uses) else None)
        if nested is None:
            problems.append(f"{where} job {name} calls {uses!r}, outside {CCA}")
        else:
            problems += callee_problems(*nested, seen, depth + 1, nightly=nightly)
    return problems


def signer_job_problems(app, jobs, pin, row):
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
    problems += forwarded_secrets_problems(app, job, name, row)
    problems += declared_secrets_problems(app, pin)
    return problems


def declared_secrets_problems(app, pin):
    """Require the 13 identity names; retain only the shared callee's optional, unforwarded PFX route."""
    where = f"{CCA} nightly-release.yml@{pin}"
    try:
        document = yaml.load(text_at(CCA, ".github/workflows/nightly-release.yml", pin), Loader=StrictLoader)
    except yaml.YAMLError as error:
        raise Refuse(f"{where} is not valid YAML: {str(error).splitlines()[0]}")
    on = document.get("on") if isinstance(document, dict) else None
    call = on.get("workflow_call") if isinstance(on, dict) else None
    spec = call.get("secrets") if isinstance(call, dict) else None
    declared = set(spec) if isinstance(spec, dict) else set()
    missing = sorted(SIGNING_NAMES - declared)
    extra = sorted(declared - SIGNING_NAMES - {"WINDOWS_CERTIFICATE", "WINDOWS_CERTIFICATE_PASSWORD"})
    problems = []
    if missing:
        problems.append(f"{where} does not declare {missing}, required by the identity mapping")
    if extra:
        problems.append(f"{where} declares secrets outside the reviewed set: {extra}")
    if isinstance(spec, dict):
        for name, declaration in spec.items():
            if not isinstance(declaration, dict) or declaration.get("required") != "false":
                problems.append(f"{where} declaration {name} must be optional (environment supplies credentials)")
    return problems


def forwarded_secrets_problems(app, job, name, row):
    """Only the pinned app targets' identity names; credentials resolve from callee environments, not copies."""
    wanted = required_secrets(row)
    if "environment" in job:
        return [f"nightly-sign.yml job {name} binds an environment; only native callee signing jobs may bind {ENV}"]
    if "secrets" not in job:
        return [f"nightly-sign.yml job {name} forwards no secrets; the exact target-specific identity mapping is required"]
    spec = job["secrets"]
    if not isinstance(spec, dict):
        return [f"nightly-sign.yml job {name} passes non-mapping secrets; only an identity mapping of the reviewed names is allowed"]
    problems = []
    for key, value in spec.items():
        if value != "${{ secrets." + key + " }}":
            problems.append(f"nightly-sign.yml job {name} forwards {key} not from secrets.{key}; identity expression required")
    extra, missing = sorted(set(spec) - wanted), sorted(wanted - set(spec))
    if extra:
        problems.append(f"nightly-sign.yml job {name} forwards secrets outside the reviewed set: {extra}")
    if missing:
        problems.append(f"nightly-sign.yml job {name} does not forward {missing}, required by the pinned app targets")
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
                if pin != "main" and not SHA40.fullmatch(pin):
                    problems.append(f"{item['name']} calls {callee}@{pin}, not a full SHA or main")
                problems += callee_problems(callee, pin, seen, nightly=True,
                                            signer=callee == "nightly-release.yml" and item["name"] == "nightly-sign.yml")
            else:
                if callee == "release.yml" and not SHA40.fullmatch(pin):
                    if OPTIONS["require_pinned_release"]:
                        problems.append(f"{item['name']} calls release.yml@{pin}, not a full SHA (--require-pinned-release)")
                    else:
                        print(f"  WARN   {app} {item['name']} calls release.yml@{pin}, not a full SHA: its environment guard is "
                              f"checked at today's {pin} only (pass --require-pinned-release to refuse)")
                problems += callee_problems(callee, pin, seen, nightly=item["name"] in ("nightly.yml", "nightly-sign.yml"))
    if len(pins) != 1:
        problems.append(f"caller and policy pins differ: {sorted(map(str, pins))}")
    else:
        pin = next(iter(pins))
        if not ((pin == "main" and ancestor(CCA, SIGNER_BRANCH_SUPPORT, "main")) or
                (SHA40.fullmatch(pin) and ancestor(CCA, pin, "main") and ancestor(CCA, SIGNER_BRANCH_SUPPORT, pin))):
            problems.append(f"pin {pin} is not a craft-common-actions main commit that includes #364")
        else:
            data = registry(pin)
            rows = data.get("apps") if isinstance(data, dict) else None
            row = rows.get(app) if isinstance(rows, dict) else None
            required_secrets(row)  # Refuse missing, empty or unsupported target needs before any mutation.
            problems += signer_job_problems(app, sign_jobs or {}, pin, row)
    if problems:
        raise Refuse("trust-closure check refused at " + sha[:10] + ":\n      " + "\n      ".join(problems))
    print(f"  ok     {app} main {sha[:10]}: switched on, pinned {next(iter(pins))[:10]}, signer branch {BRANCH}, trust closure clean")
    return row


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


# Rule parameters GitHub stores but leaves out of a ruleset it returns when they hold their default value: an
# `update` rule created with update_allows_fetch_and_merge false reads back with no parameters at all.
DEFAULT_RULE_PARAMETERS = {"update": {"update_allows_fetch_and_merge": False}}


def rule_parameters(rule):
    """A rule's parameters without the ones at GitHub's default, so a sent rule and its read-back compare equal."""
    defaults = DEFAULT_RULE_PARAMETERS.get(rule.get("type"), {})
    parameters = rule.get("parameters")
    if not isinstance(parameters, dict):
        return parameters  # Anything but a mapping is compared as it is, so it differs instead of vanishing.
    return {k: v for k, v in parameters.items() if not (k in defaults and v == defaults[k])}


def shape(ruleset):
    """Everything but the covered repo names. Another repository selector (repository_id, a property) stays in the
    conditions, so it differs from the canonical shape instead of failing to read."""
    bypass = sorted((b.get("actor_type"), b.get("actor_id") if b.get("actor_type") == "Team" else None, b.get("bypass_mode"))
                    for b in ruleset.get("bypass_actors") or [])
    conditions = copy.deepcopy(ruleset.get("conditions") or {})
    if isinstance(conditions.get("repository_name"), dict):
        conditions["repository_name"].pop("include", None)
    rules = sorted(json.dumps({"type": r.get("type"), **({"parameters": p} if (p := rule_parameters(r)) else {})}, sort_keys=True)
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
    policies = metadata_list(f"repos/{ORG}/{app}/environments/{ENV}/deployment-branch-policies", "branch_policies")
    # This endpoint lists branches and tags; without type a name-only row proves neither kind. Fail closed.
    if any(not isinstance(p, dict) or not isinstance(p.get("name"), str) or p.get("type") not in ("branch", "tag") for p in policies):
        raise Refuse(f"{app} UNVERIFIED environment branch admission metadata")
    return sorted((p["name"], p["type"]) for p in policies)


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


def setup_repo(app, apply, owned):
    """Per repo: secrets, branch at the switched-on main tip, then environment trust. Skip exact, refuse anything else."""
    trust = env_trust(app)
    if trust not in ([("release", "branch")], [("nightly-signer", "branch"), ("release", "branch")]):
        raise Refuse(f"{app} {ENV} environment trusts {trust}; expected only release")
    branch = gh("GET", f"repos/{ORG}/{app}/git/ref/heads/{BRANCH}", ok404=True)
    if branch is None:
        tip = main_tip(app)
        row = check_tree(app, tip)
        check_secrets(app, row)
        gh("POST", f"repos/{ORG}/{app}/git/refs", {"ref": REF, "sha": tip}, write=True, apply=apply)
    else:
        head = branch["object"]["sha"]
        # Resume only a branch our own earlier apply made: both rulesets already covered this repo when the run
        # started (so only release-managers could have created it), and it is a checked main commit.
        if owned is None:
            raise Refuse(f"{REF} already exists at {head[:10]}; whether an earlier apply made it is undetermined without the org rulesets")
        if not owned or not ancestor(f"{ORG}/{app}", head, main_tip(app)):
            raise Refuse(f"{REF} already exists at {head[:10]} and is not ours: it may be hostile. Delete it; never adopt it.")
        row = check_tree(app, head)
        check_secrets(app, row)
        print(f"  exact  {app} {REF} at {head[:10]}")
    # Trust (new or kept) only for a repo the live rulesets cover now, read after every ruleset write of this run.
    if apply and app not in live_coverage():
        raise Refuse(f"the org rulesets do not cover {app} now; not granting or keeping {BRANCH} trust (run rollback if it is trusted)")
    if ("nightly-signer", "branch") in trust:
        print(f"  exact  {app} {ENV} environment already trusts {BRANCH}")
    else:
        gh("POST", f"repos/{ORG}/{app}/environments/{ENV}/deployment-branch-policies", {"name": BRANCH, "type": "branch"}, write=True, apply=apply)


def readback(apps):
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
            if trust != [("nightly-signer", "branch"), ("release", "branch")]:
                problems.append(f"{app}: {ENV} environment trusts {trust}")
            branch = gh("GET", f"repos/{ORG}/{app}/git/ref/heads/{BRANCH}", ok404=True)
            if branch is None:
                problems.append(f"{app}: no {REF}")
            elif not ancestor(f"{ORG}/{app}", branch["object"]["sha"], main_tip(app)):
                problems.append(f"{app}: {REF} at {branch['object']['sha'][:10]} is not on main")
            else:
                check_secrets(app, check_tree(app, branch["object"]["sha"]))
                print(f"  ok     {app} {REF} at {branch['object']['sha'][:10]}")
        except Refuse as error:
            problems.append(f"{app}: {error}")
    print("\n".join(problems) or f"all settings read back exactly as planned for {len(apps)} repos")
    return 1 if problems else 0


def active_signer_run_inventory(app, max_pages=20):
    """Validated IDs and separate errors: incomplete evidence helps rollback but never grants admission."""
    found, errors = set(), []
    for status in ACTIVE_RUN_STATUSES:
        for page in range(1, max_pages + 1):
            try:
                data = gh("GET", f"repos/{ORG}/{app}/actions/workflows/nightly-sign.yml/runs?branch={BRANCH}&status={status}"
                                 f"&per_page=100&page={page}")
                runs = data.get("workflow_runs") if isinstance(data, dict) else None
                if not isinstance(runs, list) or len(runs) > 100:
                    raise Refuse("UNVERIFIED active signer-run inventory")
                invalid = False
                for run in runs:
                    if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] <= 0
                            or not isinstance(run.get("status"), str)):
                        invalid = True
                    elif run["status"] in ACTIVE_RUN_STATUSES:
                        found.add(run["id"])
                if invalid:
                    raise Refuse("UNVERIFIED active signer-run inventory entry")
            except Refuse as error:
                errors.append(f"{status} page {page}: {error}")
                break
            if len(runs) < 100:
                break
        else:
            errors.append(f"more than {max_pages * 100} {status} signer runs: refusing to guess which are active")
    return sorted(found), errors


def queued_signer_runs(app, max_pages=20):
    """Complete active inventory for advance/enable: any incomplete read fails closed."""
    runs, errors = active_signer_run_inventory(app, max_pages)
    if errors:
        raise Refuse("; ".join(errors))
    return runs


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


def check_admission(app):
    """Verify canonical organization rules, their actual branch application, and environment admission."""
    team = gh("GET", f"orgs/{ORG}/teams/{TEAM}")["id"]
    current = org_rulesets()
    expected = {}
    for body in canonical([], team):
        have = current.get(body["name"])
        if have is None or shape(have) != shape(body) or app not in covered(have):
            raise Refuse(f"canonical org ruleset {body['name']!r} does not protect {app}")
        if type(have.get("id")) is not int or have["id"] <= 0:
            raise Refuse("UNVERIFIED canonical ruleset identity")
        expected[have["id"]] = {rule["type"] for rule in body["rules"]}
    applied = []
    for page in range(1, 21):
        batch = gh("GET", f"repos/{ORG}/{app}/rules/branches/{BRANCH}?per_page=100&page={page}")
        if not isinstance(batch, list) or len(batch) > 100 or any(not isinstance(rule, dict) for rule in batch):
            raise Refuse("UNVERIFIED applied branch rules")
        applied.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise Refuse("UNVERIFIED applied branch rules pagination")
    for ruleset_id, types in expected.items():
        actual = {rule.get("type") for rule in applied if rule.get("ruleset_id") == ruleset_id
                  and rule.get("ruleset_source_type") == "Organization" and rule.get("ruleset_source") == ORG}
        if not types <= actual:
            raise Refuse(f"canonical ruleset {ruleset_id} is not applied to {REF}")
    if env_trust(app) != [(BRANCH, "branch"), ("release", "branch")]:
        raise Refuse(f"{ENV} environment does not admit exactly release and {BRANCH} branches")


def advance(app, sha, apply, enable=False):
    """Review and fast-forward without changing workflow state; explicit enable requires the reviewed head.

    A same-head retry retains every preflight. Enabling is deliberately separate from moving the branch,
    and does not restore revoked environment trust: settings must already admit the reviewed signer.
    """
    if not SHA40.fullmatch(sha or ""):
        raise Refuse("a full reviewed main commit SHA is required")
    path = f"repos/{ORG}/{app}/git/ref/heads/{BRANCH}"
    head = gh("GET", path)["object"]["sha"]
    resume = head == sha
    if enable and not resume:
        raise Refuse("enable requires --sha to match the current reviewed signer branch head; advance separately")
    if not ancestor(f"{ORG}/{app}", sha, main_tip(app)) or (not resume and not ancestor(f"{ORG}/{app}", head, sha)):
        raise Refuse(f"{sha} is not a main commit that fast-forwards {REF} from {head}")
    if not first_parent_of_main(app, sha, head):
        raise Refuse(f"{sha} is not on main's first-parent history (a commit from inside a merged branch, never main's tip)")
    state = workflow_state(app)
    if state not in ("active", "disabled_manually"):
        raise Refuse(f"{SIGNER_WORKFLOW} state {state!r} cannot be verified for advance/enable")
    check_admission(app)
    if queued_signer_runs(app):
        raise Refuse("a nightly-sign run is queued or running; it would fail closed after the move. Wait for it.")
    check_secrets(app, check_tree(app, sha))
    # Reads above can take time. Refuse a concurrent move or workflow-state change before any mutation.
    if gh("GET", path)["object"]["sha"] != head or workflow_state(app) != state:
        raise Refuse("signer branch head or workflow state changed during preflight; review again")
    check_admission(app)
    if enable:
        if state == "active":
            print(f"  exact  {app} {SIGNER_WORKFLOW} is already active at reviewed {sha[:10]}")
        else:
            try:
                gh("PUT", f"repos/{ORG}/{app}/actions/workflows/nightly-sign.yml/enable", write=True, apply=apply)
                if apply:
                    if workflow_state(app) != "active" or gh("GET", path)["object"]["sha"] != sha:
                        raise Refuse("workflow activation or reviewed head did not read back exactly")
                    check_admission(app)  # Rollback may have revoked trust during the enable write.
            except (Refuse, KeyError, TypeError, ValueError, AttributeError) as error:
                containment = disable_after_uncertain_enable(app) if apply else "no runtime write attempted"
                raise Refuse(f"PARTIAL: enable attempted; activation, reviewed head or signer admission is not verified: {error}; "
                             f"{containment}; an intervening dispatch or credential-bearing run remains possible; run rollback")
    elif resume:
        print(f"  exact  {REF} already at reviewed {sha[:10]}; workflow remains {state}")
    else:
        print(f"  review first: https://github.com/{ORG}/{app}/compare/{head}...{sha} (all of .github/ and packaging/)")
        try:
            gh("PATCH", f"repos/{ORG}/{app}/git/refs/heads/{BRANCH}", {"sha": sha, "force": False}, write=True, apply=apply)
            if apply and (gh("GET", path)["object"]["sha"] != sha or workflow_state(app) != state):
                raise Refuse("reviewed head or unchanged workflow state did not read back exactly")
        except (Refuse, KeyError, TypeError, ValueError, AttributeError) as error:
            raise Refuse(f"PARTIAL: advance attempted; branch/workflow state is not verified: {error}; run rollback")
        print(f"  note   workflow remains {state}; use explicit enable --sha after recovery if disabled")


def cancel_round(app, apply, failures, pending):
    """Cancel checked IDs, retaining earlier IDs until a complete inventory proves them gone."""
    runs, errors = active_signer_run_inventory(app)
    if not errors:
        pending.clear()
    pending.update(runs)
    failures.extend(f"listing signer runs: {error}" for error in errors)
    for run_id in sorted(pending):  # Running jobs keep secrets they already read.
        try:
            gh("POST", f"repos/{ORG}/{app}/actions/runs/{run_id}/force-cancel", write=True, apply=apply)
        except Refuse as error:
            if "HTTP 409" in str(error):
                print(f"  note   run {run_id} could not be cancelled because it is no longer running (HTTP 409)")
            else:
                failures.append(f"cancelling run {run_id}: {error}")
    return len(pending), not errors


SIGNER_WORKFLOW = ".github/workflows/nightly-sign.yml"


def signer_workflow(app):
    """The signer workflow, looked up by filename so a long workflow list cannot hide it."""
    return gh("GET", f"repos/{ORG}/{app}/actions/workflows/{SIGNER_WORKFLOW.rsplit('/', 1)[1]}", ok404=True)


def workflow_state(app):
    """The signer workflow's state: 'active', 'disabled_manually', or 'missing'."""
    workflow = signer_workflow(app)
    return "missing" if workflow is None else workflow.get("state", "unverified")


def set_workflow_enabled(app, enabled):
    workflow = signer_workflow(app)
    if workflow is None:
        raise Refuse(f"{SIGNER_WORKFLOW} is not a workflow in this repository")
    gh("PUT", f"repos/{ORG}/{app}/actions/workflows/{workflow['id']}/" + ("enable" if enabled else "disable"),
       write=True, apply=True)


def disable_after_uncertain_enable(app):
    """Two best-effort disable/readback attempts, not a lock against operators or dispatched runs."""
    failures = []
    for _ in range(2):
        try:
            gh("PUT", f"repos/{ORG}/{app}/actions/workflows/nightly-sign.yml/disable", write=True, apply=True)
        except (Refuse, KeyError, TypeError, ValueError, AttributeError) as error:
            failures.append(f"disable request: {error}")
        try:
            state = workflow_state(app)
            if state == "disabled_manually":
                return "best-effort disable read back disabled_manually (not an atomic guarantee)"
            failures.append(f"disable readback state {state!r}")
        except (Refuse, KeyError, TypeError, ValueError, AttributeError) as error:
            failures.append(f"disable readback: {error}")
    return "disable remains UNVERIFIED; signer may remain dispatchable: " + "; ".join(failures)


def rollback(app, apply, wait=None):
    """Attempt independent containment: revoke environment trust, disable dispatch, and cancel every active run.
    Failures never short-circuit later containment; report partial state rather than claiming a complete stop.
    The branch and rulesets remain, and force-cancel is asynchronous so cancellation is checked repeatedly."""
    import time
    wait = time.sleep if wait is None else wait
    failures = []
    try:
        for policy in metadata_list(f"repos/{ORG}/{app}/environments/{ENV}/deployment-branch-policies", "branch_policies"):
            if policy.get("name") == BRANCH:
                try:
                    policy_id = policy.get("id")
                    if type(policy_id) is not int or policy_id <= 0:
                        raise Refuse("unverified environment policy id")
                    gh("DELETE", f"repos/{ORG}/{app}/environments/{ENV}/deployment-branch-policies/{policy_id}", write=True, apply=apply)
                except Refuse as error:
                    failures.append(f"revoking policy: {error}")
        if apply and env_trust(app) != [("release", "branch")]:
            failures.append(f"{ENV} environment still trusts more than release")
    except (Refuse, KeyError, TypeError, ValueError, AttributeError) as error:
        failures.append(f"revoking the {BRANCH} trust: {error}")
    # Revocation blocks new credential acquisition, but running jobs may already hold credentials.
    # Independently disable dispatch and cancel every run even if either containment attempt failed.
    try:
        if apply:
            if workflow_state(app) != "disabled_manually":
                set_workflow_enabled(app, False)
                if workflow_state(app) != "disabled_manually":
                    raise Refuse("the signer workflow is still enabled after disabling it")
            print(f"  done   disabled {app} {SIGNER_WORKFLOW}; explicit enable --sha is required for recovery")
        else:
            print(f"  would  disable {app} {SIGNER_WORKFLOW} (a dispatcher could otherwise sign)")
    except (Refuse, KeyError, TypeError, ValueError, AttributeError) as error:
        failures.append(f"could not disable {SIGNER_WORKFLOW}: {error}; signer may remain dispatchable")
    pending = set()
    active, complete = cancel_round(app, apply, failures, pending)
    rounds = 1
    while apply and (active or not complete) and rounds < MAX_CANCEL_ROUNDS:
        wait(10)
        active, complete = cancel_round(app, apply, failures, pending)
        rounds += 1
    if apply and (active or not complete):
        # The last round's count was read before its cancellations: read once more before calling it a failure.
        wait(10)
        try:
            active = len(queued_signer_runs(app))
        except Refuse as error:
            failures.append(f"listing signer runs: {error}; known runs not verified stopped: {sorted(pending)}")
        else:
            if active:
                failures.append(f"{active} signer runs still active after {MAX_CANCEL_ROUNDS} cancellation rounds")
    print("  Keep the rulesets until the environment is verified; if compromise is suspected, rotate the release secrets.")
    if failures:
        raise Refuse("PARTIAL rollback; containment is not fully verified: " + "; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("plan", "apply", "readback", "advance", "enable", "rollback"))
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
    if args.mode in ("advance", "enable", "rollback"):
        if len(apps) != 1:
            sys.exit(f"{args.mode} takes exactly one repo")
        if args.mode != "rollback" and not re.fullmatch(r"[0-9a-f]{40}", args.sha or ""):
            sys.exit(f"{args.mode} needs --sha <full reviewed main commit>")
        sha = format(int(args.sha, 16), "040x") if args.mode != "rollback" else None
        try:
            if args.mode == "enable":
                advance(apps[0], sha, args.apply, enable=True)
            elif args.mode == "advance":
                advance(apps[0], sha, args.apply)
            else:
                rollback(apps[0], args.apply)
        except (Refuse, KeyError, TypeError, ValueError, AttributeError) as error:
            sys.exit(f"refused: {error}")
        return
    if args.mode == "readback":
        sys.exit(readback(apps))
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
            setup_repo(app, apply, owned_before(app, existed))
            ready.append(app)
        except Refuse as error:
            print(f"  REFUSED {app}: {error}"); blocked.append(app)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            # An unexpected response shape refuses this repo, never the remaining ones.
            print(f"  REFUSED {app}: unexpected GitHub response ({type(error).__name__}: {error})"); blocked.append(app)
    print(f"== {'applied' if apply else 'would apply'}: {', '.join(ready) or 'none'}; refused: {', '.join(blocked) or 'none'}")
    # A failed readback after apply is a failed apply.
    verified = readback(ready) if apply and ready else 0
    if apply:
        # Across every admitted repo, not just this run's: trust must never outlive ruleset coverage.
        exposed = uncovered_trust(admitted)
        if exposed:
            print(f"  EXPOSED {', '.join(exposed)}: {ENV} trusts {BRANCH} without the rulesets; run rollback for each now")
            verified = 1
    sys.exit(1 if blocked or verified else 0)


if __name__ == "__main__":
    main()

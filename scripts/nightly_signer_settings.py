#!/usr/bin/env python3
"""Settings for `refs/heads/nightly-signer` across the nightly apps. Read-only unless `apply`/`advance`/`rollback --apply`.

  plan     --repos all|a,b,...        read everything and print every write `apply` would make (needs admin:org)
  apply    --repos all|a,b,...        make them; per repo, skip what is already exact and refuse anything else
  readback --repos all|a,b,...        check every setting exactly
  advance  --repos <one> --sha S [--apply]   fast-forward one repo's branch to a reviewed main commit
  rollback --repos <one> [--apply]           cancel its signer runs and drop the environment trust

`all` means every app with targets in craft-common-actions main's nightly-apps.json. Two org rulesets, because a
ruleset's bypass covers all of its rules: the no-delete/no-force rule must live in a ruleset with no bypass at all.
A repo's branch is created at its main tip only once its switch-on PR has merged; the tip's callers, pins, policy
and trust closure are checked first. Nothing is ever forced, deleted or adopted.
"""
import argparse, base64, json, re, subprocess, sys

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


class Refuse(Exception):
    pass


def gh(method, path, body=None, write=False, apply=False, ok404=False):
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
    return None if body is None else base64.b64decode(body["content"]).decode("utf-8")


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


def check_tree(app, sha):
    """Refuse a tip whose switch-on is incomplete or where anything else can reach an environment."""
    repo, problems = f"{ORG}/{app}", []
    sign = text_at(repo, ".github/workflows/nightly-sign.yml", sha, ok404=True)
    policy_text = text_at(repo, ".github/nightly-signing-policy.json", sha, ok404=True)
    if sign is None or policy_text is None:
        raise Refuse("switch-on not merged: nightly-sign.yml or .github/nightly-signing-policy.json missing at main")
    if re.search(r"if:\s*>-?\s*\n\s*false\s*&&", sign) or "false &&" in sign:
        problems.append("nightly-sign.yml is still switched off (`false &&`)")
    if "github.ref == 'refs/heads/nightly-signer'" not in sign:
        problems.append("nightly-sign.yml does not guard on refs/heads/nightly-signer")
    pins = set(re.findall(re.escape(CCA) + r"/\.github/workflows/nightly-(?:release|unsigned)\.yml@(\S+)", sign))
    if not re.search(r"app-name:\s*" + re.escape(app) + r"\s*$", sign, re.M):
        problems.append(f"nightly-sign.yml does not pass app-name: {app}")
    policy = json.loads(policy_text)
    if policy.get("release_branch") != BRANCH:
        problems.append(f"policy release_branch is {policy.get('release_branch')!r}, not {BRANCH!r}")
    names = gh("GET", f"repos/{repo}/contents/.github/workflows?ref={sha}")
    for item in names:
        if not item["name"].endswith((".yml", ".yaml")):
            continue
        body = text_at(repo, item["path"], sha)
        if re.search(r"(?m)^\s*environment\s*:", body):
            problems.append(f"{item['name']} binds an environment directly")
        for callee, pin in re.findall(r"uses:\s*" + re.escape(CCA) + r"/\.github/workflows/([\w.-]+\.ya?ml)@(\S+)", body):
            if callee == "nightly-release.yml" and item["name"] != "nightly-sign.yml":
                problems.append(f"{item['name']} calls nightly-release.yml; only nightly-sign.yml may")
            if callee in ("nightly-release.yml", "nightly-unsigned.yml"):
                pins.add(pin)
                if not re.fullmatch(r"[0-9a-f]{40}", pin):
                    problems.append(f"{item['name']} calls {callee}@{pin}, not a full SHA")
            if callee == "release.yml":
                callee_text = text_at(CCA, ".github/workflows/release.yml", pin)
                envs = re.findall(r"(?m)^\s*environment:\s*(.+?)\s*$", callee_text)
                if not envs or any(value != GUARD for value in envs):
                    problems.append(f"{item['name']} calls release.yml@{pin}, which lacks the stable-only guard")
    pins |= {policy.get("producer_tooling_revision"), policy.get("signer_tooling_revision")}
    if len(pins) != 1:
        problems.append(f"caller and policy pins differ: {sorted(map(str, pins))}")
    else:
        pin = next(iter(pins))
        if not (re.fullmatch(r"[0-9a-f]{40}", pin or "") and ancestor(CCA, pin, "main") and ancestor(CCA, SIGNER_BRANCH_SUPPORT, pin)):
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
    bypass = sorted((b["actor_type"], b["actor_id"] if b["actor_type"] == "Team" else None, b["bypass_mode"]) for b in ruleset.get("bypass_actors") or [])
    conditions = json.loads(json.dumps(ruleset["conditions"])); conditions["repository_name"].pop("include", None)
    rules = sorted(json.dumps({"type": r["type"], **({"parameters": r["parameters"]} if r.get("parameters") else {})}, sort_keys=True) for r in ruleset["rules"])
    return {"target": ruleset["target"], "enforcement": ruleset["enforcement"], "conditions": conditions, "rules": rules, "bypass": bypass}


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
    team = gh("GET", f"orgs/{ORG}/teams/{TEAM}")["id"]
    try:
        current = org_rulesets()
    except Refuse as error:
        if apply:
            raise
        # A plan with a token lacking admin:org still checks every repo; apply always needs the real read.
        print(f"  NOTE   cannot read org rulesets with this token ({error}); planning as if neither exists")
        current = {}
    for body in canonical(apps, team):
        have = current.get(body["name"])
        if have is None:
            gh("POST", f"orgs/{ORG}/rulesets", body, write=True, apply=apply); continue
        if shape(have) != shape(body):
            raise Refuse(f"org ruleset {body['name']!r} differs from the canonical one; reconcile by hand, never overwrite")
        covered = have["conditions"]["repository_name"]["include"]
        missing = sorted(set(apps) - set(covered))
        if not missing:
            print(f"  exact  org ruleset {body['name']!r} already covers {len(apps)} repos"); continue
        have["conditions"]["repository_name"]["include"] = sorted(set(covered) | set(apps))  # Only the repo list changes.
        update = {k: have[k] for k in ("name", "target", "enforcement", "conditions", "rules", "bypass_actors")}
        gh("PUT", f"orgs/{ORG}/rulesets/{have['id']}", update, write=True, apply=apply)
    return current


def setup_repo(app, apply, rulesets_existed, row):
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
        # Resume only a branch our own earlier apply made: the rulesets covered it, and it is a checked main commit.
        if not rulesets_existed or not ancestor(f"{ORG}/{app}", head, main_tip(app)):
            raise Refuse(f"{REF} already exists at {head[:10]} and is not ours: it may be hostile. Delete it; never adopt it.")
        check_tree(app, head)
        print(f"  exact  {app} {REF} at {head[:10]}")
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
            problems.append(f"{body['name']!r} differs: {shape(have)}")
        missing = sorted(set(apps) - set(have["conditions"]["repository_name"]["include"]))
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


def queued_signer_runs(app):
    runs = gh("GET", f"repos/{ORG}/{app}/actions/workflows/nightly-sign.yml/runs?branch={BRANCH}&per_page=100", ok404=True) or {"workflow_runs": []}
    return [r["id"] for r in runs["workflow_runs"] if r["status"] in ("queued", "in_progress", "waiting", "pending", "requested")]


def advance(app, sha, apply):
    head = gh("GET", f"repos/{ORG}/{app}/git/ref/heads/{BRANCH}")["object"]["sha"]
    if not (ancestor(f"{ORG}/{app}", sha, main_tip(app)) and ancestor(f"{ORG}/{app}", head, sha) and head != sha):
        raise Refuse(f"{sha} is not a main commit that fast-forwards {REF} from {head}")
    if queued_signer_runs(app):
        raise Refuse("a nightly-sign run is queued or running; it would fail closed after the move. Wait for it.")
    check_tree(app, sha)
    print(f"  review first: https://github.com/{ORG}/{app}/compare/{head}...{sha} (all of .github/ and packaging/)")
    gh("PATCH", f"repos/{ORG}/{app}/git/refs/heads/{BRANCH}", {"sha": sha, "force": False}, write=True, apply=apply)


def rollback(app, apply):
    for run_id in queued_signer_runs(app):  # Running jobs keep secrets they already read.
        gh("POST", f"repos/{ORG}/{app}/actions/runs/{run_id}/force-cancel", write=True, apply=apply)
    for policy in gh("GET", f"repos/{ORG}/{app}/environments/{ENV}/deployment-branch-policies?per_page=100")["branch_policies"]:
        if policy["name"] == BRANCH:
            gh("DELETE", f"repos/{ORG}/{app}/environments/{ENV}/deployment-branch-policies/{policy['id']}", write=True, apply=apply)
    if apply and env_trust(app) != [("release", "branch")]:
        raise Refuse(f"{ENV} environment still trusts more than release")
    print("  Keep the rulesets until the environment is verified; if compromise is suspected, rotate the release secrets.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("plan", "apply", "readback", "advance", "rollback"))
    parser.add_argument("--repos", required=True, help="all, or a comma-separated list of app names")
    parser.add_argument("--sha"); parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    reg = registry()
    admitted = registry_apps(reg)
    apps = admitted if args.repos == "all" else sorted(set(args.repos.split(",")))
    unknown = [a for a in apps if a not in admitted or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a)]
    if unknown:
        sys.exit(f"not admitted in craft-common-actions main nightly-apps.json: {unknown}")
    if args.mode in ("advance", "rollback"):
        if len(apps) != 1:
            sys.exit(f"{args.mode} takes exactly one repo")
        if args.mode == "advance" and not re.fullmatch(r"[0-9a-f]{40}", args.sha or ""):
            sys.exit("advance needs --sha <full reviewed main commit>")
        try:
            advance(apps[0], args.sha, args.apply) if args.mode == "advance" else rollback(apps[0], args.apply)
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
            setup_repo(app, apply, all(name in existed for name in (RULESET_WRITERS, RULESET_HISTORY)), reg["apps"][app])
            ready.append(app)
        except Refuse as error:
            print(f"  REFUSED {app}: {error}"); blocked.append(app)
    print(f"== {'applied' if apply else 'would apply'}: {', '.join(ready) or 'none'}; refused: {', '.join(blocked) or 'none'}")
    if apply and ready:
        readback(ready, reg["apps"])
    sys.exit(1 if blocked else 0)


if __name__ == "__main__":
    main()

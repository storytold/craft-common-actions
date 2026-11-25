# Reviewed baseline CI gate (private, opt-in)

This is a private successor to the v7.2 strict-green policy. Nothing is published
or activated. The existing v1 schema still requires every configured independent
check and its whole workflow to succeed. It never gains an implicit exemption.
The v2 schema adds explicitly reviewed authenticated main-failure handling and
narrow applicability adapters. The integrated defining-job bootstrap accepts v2
only when its canonical digest matches the independently configured review root.
Caller/publication/attestation/pruning/Unix execution remain off; this document
is not authority to activate any of them.

## Trust boundary and setup

1. A trusted maintainer selects the current main SHA through authenticated GitHub
   reads, reviews the complete job/check inventory and exact workflow configuration,
   and pins each baseline run ID, number, attempt, job ID, check ID and result.
   Do not use candidate output, PR/fork checks, status text, a bot login, a check
   name by itself, or the old QA packet to choose exemptions. Review transitive
   reusable workflow/action references too. This adapter pins the caller Git blob
   AND the authenticated resolved reusable-workflow path/SHA set for every quality
   run. It does not claim to bind every action used transitively by existing CI;
   those dependencies still need applicable review before activation.
2. The reviewed JSON must arrive from an independently trusted immutable snapshot.
   Compute its canonical digest with `nightly_policy.config_digest(config)`.
   Bind `NIGHTLY_REVIEWED_POLICY_SHA256` as the independently reviewed repository
   variable available to the identity, status and publication jobs. A value kept
   only in the signing environment is not available to those jobs. The signing
   environment protects actual signing secrets, not this global public digest. It must **not** be
   computed from candidate-controlled JSON at runtime, taken from dispatch/caller
   input, or supplied by an artifact. Config plus its self-reported digest is not
   authentication. Repository/environment setup is NOT performed by this change.
   An attacker who can replace the trusted controller/review root is outside this
   boundary; this code cannot manufacture evidence that a human reviewed a file.
3. Review before dispatching a new producer run. Review age is bounded to at most
   24 hours, expires at the explicitly recorded UTC instant, and may not postdate
   producer creation. A re-run cannot silently adopt a policy reviewed later.
4. Baseline must be current candidate main itself (no-change nightly), or its
   immediate **first parent**. Older ancestors, even within the age limit, refuse.
   If main has advanced further, refresh the reviewed snapshot and dispatch again.
   There is deliberately no automatic search for an easier historical main run.
5. A refresh must call `validate_baseline_refresh(previous, updated, policy, api)` with
   the actual previously approved immutable v2 snapshot, then preserve that review
   history. Never substitute a candidate's claimed previous document. Coverage
   changes, including managed-check additions/removals or workflow replacements,
   require an explicit separate policy migration and equivalence review;
   they cannot pass through the normal refresh helper. The setup controller must
   enforce this helper; the runtime has no trusted policy-history storage itself.
   The helper authenticates up to 20 intervening first-parent main revisions,
   their complete exact-source job/check/run histories, and requires the new
   green floor to include every recovered check. Missing history, workflow
   configuration drift, divergent ancestry or a reached bound requires explicit
   reconciliation; it never silently skips intervening main revisions.

No hosted calls, settings, secrets or policy pins have been installed. These are
mandatory integration conditions, not evidence of a deployed trust root.

## Exact runtime schema

All objects reject unknown/missing fields. This illustrative fixture is **not a
production approval**: every revision and numeric identity below is synthetic.
The existing `tooling_revision` must still equal the immutable defining tooling
revision. Real independent workflow/check/job identities must come from reviewed
authenticated responses, never these values.

```json
{
  "schema": "craft-nightly-publication-policy/v2",
  "tooling_revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "apps": {
    "photocraft": {
      "producer_workflow": {"id": 91, "path": ".github/workflows/nightly.yml"},
      "required_checks": [
        {
          "name": "fixture quality check",
          "workflow_id": 72,
          "app_id": 18,
          "workflow_path": ".github/workflows/fixture-ci.yml",
          "workflow_blob": "dddddddddddddddddddddddddddddddddddddddd",
          "green_required": false,
          "baseline_job_id": 98,
          "baseline_check_id": 201,
          "baseline_conclusion": "failure"
        }
      ],
      "baseline": {
        "schema": "craft-nightly-main-baseline/v1",
        "repository": "storytold/photocraft",
        "branch": "main",
        "source_commit": "cccccccccccccccccccccccccccccccccccccccc",
        "reviewed_at": "2026-11-21T12:00:00Z",
        "expires_at": "2026-11-22T12:00:00Z",
        "runs": [{"workflow_id": 72, "id": 1111, "attempt": 1, "number": 16}]
      }
    }
  }
}
```

`required_checks` is the complete covered independent Actions job inventory,
including jobs reviewed red: it is **not** a candidate-derived ignore list.
Names must be unique within each authenticated workflow; equal display names
in different workflow IDs are distinct obligations. Each is additionally authenticated through the exact job's
`check_run_url`, the check ID/app ID/source, the run/attempt, workflow ID/path/blob,
trusted repository and main event. There may be 1–128 jobs and 1–32 workflows.
Each baseline job/check ID appears once. Ordinary baseline results are only
`success` or `failure`, and `success` mandates `green_required: true`. The sole
reviewed `skipped` exception is the explicit `manual-input-only` adapter described
below; it does not count as passing coverage.

The prepared policy adds `quality_policy_sha256` (64 lowercase hex characters)
to the existing exact v6 policy fields. The identity/feed format is unchanged.
At final validation the digest must still match both config and the independently
trusted review digest. Thus replacing the config between prepare and publish, or
downgrading v2 to v1, refuses. Prior released policies retain their recorded digest;
old published policies need not match a newer reviewed config to prove ordering.

## Live witness and pass rules

Every preflight and both existing publication guards reconstruct a fresh witness
through the authenticated fixed-host transport. No untrusted witness JSON is
accepted. The witness is bounded to 640 GET requests and a 120-second collection
budget (plus existing transport timeouts; preflight retains its real alarm).
Counted 32-workflow/128-job two-SHA synthetic paths used 607/617 GETs, including
final rechecks. Excessive history still refuses. This per-witness cap does not
prove the combined preflight/publication flow fits the repository's hourly API
quota; a refusal is never permission to skip authentication.
Inventories use pages of 20, at most 20 pages each, with stable exact integer
`total_count`, complete final-page coverage and duplicate-ID rejection. The
attempt-history bound is 20 attempts per run; hitting any bound refuses rather
than truncating or selecting an old result. Transport errors, including 404,
401/403 and throttling, never become exemptions.

For baseline and candidate, select the latest independent run by trusted workflow
run number, never opaque run ID. Re-read the selected root and attempt, verify
active workflow ID/path and the pinned Git blob at **both exact source SHAs**,
then enumerate **all jobs** of that exact attempt. A job must link to a check
from the exact commit's check inventory and have a fixed GitHub job URL.
Both job and check must be completed with matching `success` or `failure`.
The aggregate workflow must be completed and agree with its full job results.
A workflow can therefore legitimately be red because of a reviewed baseline-red
job without throwing away its other passing job obligations.

| Reviewed baseline | Live baseline | Green floor/history | Candidate |
| --- | --- | --- | --- |
| success | success | required | must succeed |
| success | failure | any | refuse baseline drift; never grant exemption |
| failure | failure | never green | success or demonstrated failure allowed |
| failure | success | newly required | must succeed |
| failure | failure | previously green | must succeed |
| missing/unknown/unreviewed skipped/pending/cancelled | any | any | refuse |

Historical successful jobs on either exact main revision (all bounded trusted runs
and attempts, exact-ID check reads) also set the live green floor, including
completed successful jobs in a cancelled or partial historical attempt. Unknown
or incomplete historical jobs grant no positive evidence; selected baseline and
candidate runs still require their complete inventory and aggregate validation.
A later red
rerun cannot hide an earlier successful main result. A refresh must preserve every
previous `green_required` bit; when its new baseline succeeds the bit must become
true. Authenticated refresh history prevents jumping over a recovered main revision,
including when refreshing directly to the new current main SHA. There is no
forever-grandfathered exception after recovery.

Unknown jobs/checks, changed names or workflow coverage, and same-name impostors
refuse. The only omitted checks may be the producer's own jobs, and those require
authenticated producer run identity plus exact job-to-check linkage; a forged
producer URL does not exempt a check. External SonarCloud checks are NOT Actions
jobs and need a separately reviewed adapter/applicability policy, never a fake
workflow ID. There is intentionally no general skip/neutral/external allowlist.

At the end, select the latest roots again (no rerun/run-number race), check witness
expiry again, and re-read main at the exact candidate SHA. The final publication
guard rechecks main, then the active producer, then the newest producer run as
its final remote observation. A no-API baseline-expiry check follows that read.
The existing serialized final same-head checks remain. GitHub offers no
atomic transaction spanning these reads and a release mutation: a subsequent
administrator change remains a cooperative race, not an atomic lock guarantee.

## Applicability adapter and remaining adoption work

The private v2 adapter now represents these reviewed cases explicitly:

- `applicability: "manual-input-only"` on an Actions obligation requires a reviewed skipped baseline and `green_required:false`, with the exact caller workflow Git blob and resolved reusable-workflow path/SHA inventory bound. It permits only a **skipped** result on an authenticated push/schedule run. It is not success coverage, not an allow-list based on a check name and not permission to skip a baseline-green obligation. A manual run, changed workflow/callee, unannotated skip, pending/cancelled result or generic path-filter declaration refuses. Classify only conditions actually reviewed as input-only; Photo’s seven existing explicit manual-input jobs were inspected from its real CI source.
- `external_checks` supports only the actual Photo `SonarCloud Code Analysis` app identity (numeric app12526), exact project/main URL and exact source SHA. The repository-scoped authenticated GitHub check result—not the separate successful Actions Scan job—establishes success/failure. Completed baseline and candidate checks are read by exact ID again; missing/duplicate/wrong-app/source/URL/skipped/pending results refuse. Baseline success requires candidate success; only a verified reviewed failure may remain failure, and recovery removes that exemption. An arbitrary provider/name/endpoint cannot be supplied.
- `managed_checks` explicitly accounts for GitHub’s optional dynamic Dependabot dependency-update workflow. It requires its independently reviewed numeric workflow ID, exact dynamic path, official Actions app ID, same-repository main/source/run/job/check linkage and completed **success** when present. Failure is never exempted, and unknown extra checks still refuse.
- Equal display names from different authenticated workflow IDs are separate obligations; same-workflow duplicates remain invalid. Every quality run’s resolved referenced-workflow path/SHA set must equal the reviewed policy even if its caller Git blob is unchanged. This binds reusable workflow resolutions; it does not claim that every action used transitively by existing CI was independently immutable.

The earlier historical audit was not an approved fresh baseline. Real read-only current-main checks and source are in the review packet’s evidence directory; Sonar’s separate external quality gate must not be called green from a successful scanner job. No profile-dropping workaround, unrelated CI source fix or real signing/hosted producer acceptance was performed.

Activation still requires exact independently reviewed current policy values/digest, actual public producer/caller pins and hosted validation. The conservative refresh helper preserves green obligations and refuses an external-policy refresh across intervening main history pending explicit reconciliation; it is not an installed controller and no automated policy-refresh service or daily user-review workflow is claimed. The initial separately approved manual run can use a fresh current-main snapshot. No arbitrary historical baseline, generic skipped-check exemption or permanent red exemption is activated by these source helpers.

## Integration and validation

Cherry-pick the private commit; changes are confined to `scripts/nightly_policy.py`,
its new focused baseline tests, and this document. Existing exact workflow/app,
self-cycle, producer-active, permissions and freshness checks remain in place.
The parent owns any pre-checkout/bootstrap/controller policy-root wiring, caller
permission review and final combined suite. Keep publishing disabled. Normal
v1 callers/tests need no changed input.

Focused local command (run through the normal task allocator with 1 GiB and SSD
TMPDIR):

```
python3 -B -m unittest discover -s tests -p 'test_nightly_policy*.py' -v
```

These are synthetic/API-fake regression and adversarial tests, not authenticated
hosted execution or native application proof. Commands/raw results and cleanup
receipts are retained outside build roots at
`/home/codex/work/ssd2/task-evidence/nightly-ci-baseline/`.

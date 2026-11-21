# Reviewed baseline CI gate (private, opt-in)

This is a private successor to the v7.2 strict-green policy. Nothing is published
or activated. The existing v1 schema still requires every configured independent
check and its whole workflow to succeed. It never gains an implicit exemption.
The v2 schema below adds **only an explicitly reviewed, authenticated main-failure
exception**. No workflow, publisher permission, signing or deletion change is made
here. The existing pre-checkout bootstrap deliberately still accepts v1 only;
the integration owner must review any v2 wiring separately.

## Trust boundary and setup

1. A trusted maintainer selects the current main SHA through authenticated GitHub
   reads, reviews the complete job/check inventory and exact workflow configuration,
   and pins each baseline run ID, number, attempt, job ID, check ID and result.
   Do not use candidate output, PR/fork checks, status text, a bot login, a check
   name by itself, or the old QA packet to choose exemptions. Review all transitive
   reusable workflow/action references too: this adapter pins the top-level
   workflow Git blob, not the contents behind an imported mutable ref. Such mutable
   transitive references need immutable pins/review before activation.
2. The reviewed JSON must arrive from an independently trusted immutable snapshot.
   Compute its canonical digest with `nightly_policy.config_digest(config)`.
   Bind `NIGHTLY_REVIEWED_POLICY_SHA256` in the trusted controller (for example an
   independently protected reviewed environment value). It must **not** be
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
   changes require an explicit separate policy migration and equivalence review;
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
Names must be unique; each is additionally authenticated through the exact job's
`check_run_url`, the check ID/app ID/source, the run/attempt, workflow ID/path/blob,
trusted repository and main event. There may be 1–128 jobs and 1–32 workflows.
Each baseline job/check ID appears once. Baseline results are only `success` or
`failure`, and `success` mandates `green_required: true`.

The prepared policy adds `quality_policy_sha256` (64 lowercase hex characters)
to the existing exact v6 policy fields. The identity/feed format is unchanged.
At final validation the digest must still match both config and the independently
trusted review digest. Thus replacing the config between prepare and publish, or
downgrading v2 to v1, refuses. Prior released policies retain their recorded digest;
old published policies need not match a newer reviewed config to prove ordering.

## Live witness and pass rules

Every preflight and both existing publication guards reconstruct a fresh witness
through the authenticated fixed-host transport. No untrusted witness JSON is
accepted. The witness is bounded to 256 GET requests and a 120-second collection
budget (plus existing transport timeouts; preflight retains its real alarm).
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
| missing/unknown/skipped/pending/cancelled | any | any | refuse |

Historical successful jobs on either exact main revision (all bounded trusted runs
and attempts, exact-ID check reads) also set the live green floor. A later red
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
guard rechecks main again immediately before the preserved last active-producer
read. The existing serialized final same-head checks remain. GitHub offers no
atomic transaction spanning these reads and a release mutation: a subsequent
administrator change remains a cooperative race, not an atomic lock guarantee.

## Known adoption blockers and Sonar evidence

The existing QA packet at
`/home/codex/work/task-evidence/photo-nightly-ci-policy-20261121/` is historical,
not a fresh runtime baseline. Its main source is recorded in `MAIN-REF.json`.
`PRIVATE-POLICY-PROPOSAL.md` proposes only three aggregate contexts under v1;
that is not a complete v2 inventory. Its CI push has successful without-HEIF,
corpus and web jobs, while other jobs are skipped. Its newer same-source schedule
has more skipped contexts. The strict complete-inventory v2 adapter intentionally
refuses those unknown/skipped obligations rather than calling skips green or
falling back to an older easier push. Activation needs reconciled triggers or an
explicit, separately reviewed applicability adapter. It also needs a decision
and implementation for the separate external SonarCloud app, all real immutable
pins, trusted review-root wiring, a retained refresh history and hosted validation.

`SONAR-JOBS.json` identifies the final Actions job's failing step as **Fail on
Rust shard test failures**. The **SonarCloud Scan step succeeded**, and all four
instrumented shard job conclusions succeeded. The sentinel reports shard failures
separately. No retained packet log contains the individual failing assertion or
its source root cause, so there is no evidenced source-only Sonar fix here. Do
not alter frozen Photo source, suppress the sentinel, or call scanner success an
external SonarCloud quality-gate pass. No native/hosted success is claimed.

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
python3 -m unittest discover -s candidate/craft-common-actions/tests -p 'test_nightly_policy*.py' -v
```

These are synthetic/API-fake regression and adversarial tests, not authenticated
hosted execution or native application proof. Commands/raw results and cleanup
receipts are retained outside build roots at
`/home/codex/work/ssd2/task-evidence/nightly-ci-baseline/`.

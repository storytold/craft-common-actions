# Keep-seven pruning integration (private draft, off by default)

This is new private source, not hosted proof or permission to enable deletion.
The workflow/publisher owner wires it **after successful publication** in the
same repository-serialized publishing transaction. Failed/skipped publication
must never invoke it. A hard-off workflow switch remains until explicit first-run
approval. No stable workflow or policy file is changed by this helper.

## Interface

`nightly_prune.plan(api, registry, app, current_release_id, bot_id, provenance)`
is read-only. `execute(api, registry, plan, approved=True, provenance=provider,
checkpoint=writer)` requires an explicit approval and a durable checkpoint
callback. `PruneTransport` preserves the publisher transport and adds exact empty
204 deletion and bounded artifact ZIP download. The callable adapter is
`nightly_ownership.ActionsOwnership(reviewed_config)`; no raw receipt JSON is
accepted as authority. Without it all unknown provenance is retained.

Example **reviewed configuration shape**, not deployable values (replace every
example ID/name/pin with verified approved values, including the historical pins):

```json
{
  "schema": "craft-nightly-ownership-policy/v1",
  "repository": "storytold/photocraft",
  "repository_id": 123,
  "bot_id": 456,
  "producer_workflow_id": 789,
  "producer_workflow_path": ".github/workflows/nightly.yml",
  "publish_job_name": "Nightly / publish-nightly",
  "publish_step_name": "Publish",
  "upload_step_name": "Upload nightly ownership receipt",
  "tooling_workflow_path": "storytold/craft-common-actions/.github/workflows/nightly.yml",
  "allowed_tooling_revisions": ["1111111111111111111111111111111111111111"]
}
```

Draft CLI, with `GH_TOKEN` supplied only to this trusted publisher boundary;
requires `actions: read` plus `contents: write` in that app repository. Run from
reviewed tooling; config must come from reviewed source, not a build artifact.
The commands below are **not run live by this work**:

```text
python3 scripts/nightly_prune.py plan --registry nightly-apps.json --app photocraft --current-release-id RELEASE_ID --bot-id BOT_ID --config ownership-policy.json
python3 scripts/nightly_prune.py apply --registry nightly-apps.json --app photocraft --current-release-id RELEASE_ID --bot-id BOT_ID --config ownership-policy.json --plan prune-plan.json --checkpoint prune-checkpoint.json --checkpoint-artifact-id CHECKPOINT_ARTIFACT_ID --checkpoint-artifact-sha256 CHECKPOINT_ARTIFACT_SHA256 --approve-first-run
```

Save plan stdout to `prune-plan.json`, then upload those exact bytes as the
trusted checkpoint artifact in the current run and attempt before apply. Pass its
exact artifact ID and digest from that trusted upload step, not values claimed by
the plan. The exact plan and immutable Actions artifact must be preserved on failure. The integrated CLI
accepts only a checkpoint authenticated to its **current run and attempt**.
A later Actions attempt/run cannot resume the original checkpoint. A fresh
plan enumerates releases, not orphan refs, and cannot rediscover a release
already deleted while its tag remains. Durable evidence is not an implemented
cross-attempt recovery route. Do not rerun publication to repair that pair.

`save_checkpoint` provides a local atomic write/fsync, not survival of runner
destruction. The uploaded artifact retains evidence for 90 days; it is not
itself deletion authority. Candidate eligibility and ownership still need
fresh independent authentication. Pruning must remain disabled until an
authenticated cross-attempt recovery/recheckpoint route and a full
CLI/checkpoint boundary regression have landed and been reviewed. This is an
additional implementation prerequisite before the separately approved first
live publication/pruning timing, not a request to change the keep-seven policy.
A quota-bounded ownership frontier/deletion batch is also required: the offline
49-history proof-path regression needs 1,404 authenticated requests before the
first DELETE, exceeding a normal 1,000-request/hour repository token quota.
Do not enable the current full-reproof algorithm or weaken candidate ownership
checks to make it fit. This quota design is a separate owned follow-up, not a
performance fix claimed by the current correction.

## Receipt emitted by the publisher owner

Only after exclusive ref creation returned **201**, exact ref readback, draft
asset digest verification, successful publication and final ref verification,
finish writing a single `ownership.json` with these exact fields. The publisher
reserves its new directory and opens the exclusive output file before its first
API mutation. A receipt failure after confirmed publication exits with a distinct
reconciliation error; it must never trigger a second publication/create attempt:

```json
{
  "schema": "craft-nightly-ownership/v1",
  "app": "photocraft",
  "repository": "storytold/photocraft",
  "tag": "v1.2.3-nightly.20261121.123.1.gaaaaaaaaaaaa",
  "source_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "release_id": 321,
  "run_id": 123,
  "run_attempt": 1,
  "tooling_revision": "1111111111111111111111111111111111111111"
}
```

`receipt_for(release)` specifies identity equality, not publication authority;
only the guarded publisher may emit this receipt. Upload ONLY that file as
`nightly-ownership-{app}-{run_id}-{run_attempt}` with pinned upload-artifact v4,
`overwrite: false`, missing-file failure and 90-day retention. No other job or
step may emit/overwrite that name. The trusted upload step must succeed only
when it created that exact artifact. These are requirements on every allowed
historical tooling revision, not facts inferred from receipt text.

A body discovery pointer is optional; it conveys no authority. Keep the
existing trailing producer-policy marker intact. Discovery uses authenticated
run-scoped artifact metadata, never a body-supplied URL or numeric artifact ID.

## What the provider actually proves

It checks authenticated GitHub API data for the exact historical run attempt,
main/source/event, numeric repository and approved caller workflow IDs/paths,
and referenced reusable-workflow SHA in the reviewed historical allowlist. It
requires one completed exact publish job and successful publish/upload steps,
in order, with consistent timestamps. The overall run may have failed later;
a later prune step may also have failed the publish job, while its exact publish
and upload steps still succeeded. Cancelled/incomplete jobs are rejected. The still-running **current**
publication is protected and counts as one of seven without historical receipt
completion, leaving six historical keep slots.

It finds exactly one artifact of the expected name, then reads its numeric ID
back from the API. Repository/head-repository/run IDs and full SHA must match;
expiry must be false, creation must be inside the upload step, and update no
later than job completion. It downloads only that numeric ID, follows exactly
one authenticated API redirect to a validated `productionresultssa<N>.blob.core.windows.net`
HTTPS origin **without forwarding GitHub authorization**, caps the ZIP at 128 KiB,
checks its API SHA-256 and size, and accepts exactly one regular non-symlink
`ownership.json` up to 64 KiB. Exact strict JSON identity equality is required.
Other storage origins require a reviewed allowlist update, not broad redirects.

**Important source-evidence limit:** Git refs have no lightweight-tag creator.
REST artifact metadata identifies a workflow run, not the uploading job/step.
Job origin here relies on the reviewed exclusive upload-v4 creation code,
successful step and timing, alongside authenticated run origin and digest. It is
not a platform-signed job attestation or cryptographic proof of tag authorship.
A malicious actor who can alter trusted workflow/tooling, compromise the upload
step/runtime, or manipulate same-run artifact APIs is outside this source-bound
proof. Do not use tagger metadata, login, release text, body pointers, a matching
ref or a workflow run alone as ownership. No adequate receipt, no deletion.
Legacy/expired/missing/unreviewed receipts are retained with reasons; ambiguity
or an incomplete bounded origin inventory aborts/defer the whole plan.

## Deletion safety and operational limits

- Complete release inventory: at most 20 pages of 20 (same 400-row ceiling as
  current producer policy); full final page, invalid/duplicate rows or auth/rate
  errors stop. It does not silently use only the newest 30 releases. No global
  orphan-ref enumeration or broad tag deletion exists.
- Exact nightly tags only, real calendar dates, numeric run/attempt ordering,
  independent of tag date/base SemVer. Run/attempt ties defer the whole plan.
  Stable, manual/foreign, drafts, malformed, unrelated-app and unknown records
  are never deleted. Numeric authenticated Bot author plus immutable published
  prerelease/source identity are necessary but not sufficient.
- Keep current plus six newest proven historical nightlies; propose at most ten
  older removals, oldest first. Unknown/legacy releases stay in addition to these.
- Recompute fresh eligibility and validate **all** planned candidates before any
  delete; then revalidate the remaining plan and candidate before each deletion.
  Mutate only the fully validated plan, never a newly discovered orphan.
- Release first. Stop on errors/uncertain responses. Require explicit confirmed
  404 absence by ID AND tag before deleting the exact still-matching lightweight
  ref. Recheck receipt and SHA; confirm tag absence. Release-delete failure never
  authorizes tag deletion. The executor can continue its own release-gone/tag-left
  pair only with the original authenticated receipt and exact preserved plan.
  This function-level behavior is not a later-attempt CLI/workflow recovery route;
  the mandatory checkpoint verifier deliberately rejects that changed origin.
- GitHub documents that an immutable release **can** be deleted, then its tag
  can be deleted, but its name cannot be reused. This is not an atomic GitHub
  transaction. Git ref DELETE has no SHA compare-and-delete precondition; races
  after the last read cannot be eliminated by this helper. Serialize with all
  publishers and restrict independent tag mutation/settings changes. If that
  administrative assumption is unavailable, leave apply disabled.
- Bounds are ceilings, not runway promises. Repeated fresh API/ZIP proofs can
  hit rate limits on a backlog; errors stop and preserve remaining entries.
  Receipt expiry (90 days), absent/ambiguous proofs, protected drafts/unknown
  backlog and incomplete pagination need explicit owner reconciliation, not
  relaxed ownership checks or ad hoc deletion.
- Keeping seven qualified nightlies does **not** guarantee the old stable
  Launcher's 30-release window contains stable: preserved unknown releases,
  drafts, other prereleases and backlog may still crowd it out. Observe/resolve
  that separate rollout limitation before unattended daily publication.

## Interrupted pruning: stop and separately reviewed operator procedure

This is the incident preparation/review procedure, **not an implemented repair
command or advance authorization to delete a tag**. Infra owns recovery tooling;
the sole publisher performs any separately approved hosted mutation.

1. Stop retries and retain the original plan artifact ID, API digest, archive,
   exact plan bytes, repository/run/attempt/head/callee identity, ownership
   receipts and the release/ref API responses. Keep publication/pruning serialized;
   wait for active attempts rather than cancelling unrelated work. Do not replace
   the plan with a fresh inventory or overwrite the historical artifact.
2. Under the reviewed historical tooling/ownership policy, independently
   authenticate that original artifact and its origin. Read-only reconciliation
   must establish the exact candidate is in that authenticated plan, the original
   publication receipt still authenticates, the release is absent by both numeric
   ID and exact tag, and the remaining lightweight ref still names the recorded
   source SHA. A different/missing/expired/ambiguous proof means retain/refuse.
3. Prepare a separately reviewed recovery change that either verifies the
   independently identified historical origin, or re-checkpoints those exact
   authenticated plan bytes in a new trusted attempt under the same publication
   serialization. Do not hand-edit the plan/checkpoint or relax the current
   verifier's run/attempt checks. No such route is supplied in this preview.
4. Before any mutation, independently review that exact recovery change and its
   full CLI/checkpoint regression: delete release, interrupt before tag deletion,
   then recover in a different attempt; reject changed artifact bytes/origin,
   unknown orphan refs, expired evidence and stale ownership/ref/keep-set proofs.
   Re-run all existing fresh candidate/ref/keep-seven checks. Present the exact
   bounded live operation and results for the required publication approval.
5. Only the reviewed, explicitly approved recovery route may remove the exact
   leftover tag; confirm the release remains absent and the exact tag is gone.
   Never adopt an arbitrary orphan, reuse a deleted immutable tag name, or treat
   a read-only incident packet/manual receipt as deletion authority. If that route
   is unavailable, leave the tag intact and record the unresolved pair.

## Evidence scope

The tests are offline adversarial API/ZIP mocks and source integration checks.
No hosted run, live deletion, immutable-setting test, real bot receipt, native
platform or release-signing proof is claimed. Parent integrates publisher/workflow;
this slice deliberately does not modify either or any frozen fixture metadata.

References checked 2026-11-21:
- https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases
- https://docs.github.com/en/rest/actions/artifacts
- https://docs.github.com/en/rest/actions/workflow-runs
- https://docs.github.com/en/rest/actions/workflow-jobs
- https://docs.github.com/en/rest/git/refs
- https://docs.github.com/en/rest/repos/repos#list-repository-activities
- https://docs.github.com/en/rest/activity/events (recent finite, delayed events are not a durable creator ledger)

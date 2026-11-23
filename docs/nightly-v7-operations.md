# v7.1 bounded operational notes — proposed, not activation authority

Use only the dedicated app `.github/workflows/nightly.yml` caller for installable
Photo nightlies. Stable release dispatches and independent diagnostic runs must
not share that workflow ID/path or run. For shared rollout, known unadmitted apps
must use the separate diagnostic caller recipe, not repoint their old stable
release workflow to the Photo-only installable workflow. This private candidate
has not replaced any live shared workflow or migrated existing callers. There is
no automatic cross-run dispatch/routing subsystem. The exact app-owned publication policy,
its independent required check names/app IDs/workflow IDs, and real published
reviewed tooling/caller pins are still prerequisites. Empty branch contexts or
red current main are not a reason to infer/remove a QA requirement.

## CI timing and unchanged-source behavior

Each approved named check AND its whole independent producing CI workflow must
be completed with success on the exact source. A green named job does not suffice
while another job in that workflow runs or fails. The preflight waits at most120
seconds total, only for missing or provenance-eligible pending checks/workflows.
Red/cancelled/wrong-source/ambiguous/unapproved provenance refuses immediately;
the publication recheck does not wait and never falls back to older green CI.

A recent main commit may not finish Photo's long CI workflow before the nightly
slot, so preflight refuses without an expensive producer build. A head with no CI
(for example `[skip ci]`) keeps refusing until approved exact-source CI exists;
a missing check is not success. A new CI rerun or manual CI run on the same source
while the Windows build is accepted may make publication refuse after that build.
Preflight also runs BEFORE unchanged-source skip: red or pending CI can report a
refusal even on a night that otherwise would have skipped without a build.
Photo/QA must measure actual full CI duration and choose the eventual cron slot;
no cron choice or activation is made by this private source candidate.

## Bad-build pause and fix-forward

The nightly lead coordinates; the sole publisher owns explicitly authorized
withdrawal/public writes. These are instructions for a future authorized incident,
not commands executed by this private source task.

1. Disable the dedicated caller first using existing app workflow controls. This
   stops new triggers and gates publication; it does NOT cancel already-accepted
   builds, staging or downloads. Fresh read-only workflow-state checks at preflight
   and the last publication guard make cooperative in-flight publishers refuse.
   The future held attestation CLI checks before packet/subject work and again as
   its last API read before exposing the subject; pure subject computation stays
   offline. This still cannot stop an action after a later administrative race. An administrator can still race the final read/PATCH; there
   is no atomic administrative lock. Disabling does not cancel runs automatically.
2. Withdraw a bad release/tag only with explicit publisher scope. Record its source,
   package digest/tag, affected installed versions, and notify Photo/Launcher/QA.
   Deletion alone is NOT a pause; it can expose an older prior-nightly comparison.
   A refused final publication can leave a public source tag plus its owned draft
   containing the ZIP. Preserve exact tag/source/draft/package evidence for the
   sole publisher's incident cleanup under explicit scope. Never automatically
   reuse or delete that tag/draft, including on a retry.
3. Wait for ALL prior nightly runs to terminate, or use only separately authorized
   incident cancellation. Never re-run a bad run: reruns build its original commit,
   not a new fixed commit. No blanket cancellation/deletion authority is implied.
4. Verify the fixed source/package privately, merge the fix under the normal review
   process, and get the approved exact-source checks green. Then explicitly
   re-enable the dedicated workflow and promptly dispatch a NEW main run. Do not require a
   paused hosted producer run to succeed before re-enabling; that is circular.
5. Verify the new source/package/admission and actual publication independently.
   Never clobber a prior tag/draft or silently promise rollback/downgrade. Existing
   installed copies and user data are not deleted by pausing or opting out.

## Finite bounds and explicit reconciliation

The release scan reads at most 20 pages x20 releases with bounded page bodies.
A shorter history is read completely, as before. A longer one (GitHub serves only
a bounded prefix of the list, with no documented order) is not treated as a
complete catalog: the newest nightly in the window names the stable release it
was built on, and that release is re-read by exact ID and must still be the same
published stable tag. A newer stable inside the window still wins, and the
newest nightly still goes through the full prior/freshness checks. A full window
with no nightly, or an anchor that is gone, changed or malformed, refuses. It
never grows memory/page sizes or prunes to pass. The step summary names
catalog-bound, unavailable prior run, lineage or version-regression refusals.
Diagnostics remain independent even if any producer condition refuses.

For a deleted prior Actions run, renamed/recreated caller workflow, withdrawn
stable base or unanchored long catalog, the nightly lead/QA/publisher must own
a separately reviewed migration: preserve prior tag/source/digest/policy evidence,
choose the explicit new trusted lineage and valid monotonic base/identity, review
compatibility and the exact validation/rollback boundary, then authorize only that
migration. Current code stays paused/refusing until such source/policy work is
reviewed. Force is only a rebuild/unchanged-source override, never a guard bypass.
No delete-history recipe, arbitrary baseline reset, guessed lineage, index,
retention, ruleset/admin or catalog-scaling mechanism is supplied here.

## Producer bindings and future activation

Expected identity, ZIP and packet hashes come from producing-job outputs, not the
downloaded artifacts. They are independent of replaceable artifact storage, NOT
independent of the build job or its smoke execution. The Windows build can write
its workspace/tooling; its digest proves what that build emitted, not independent
build/smoke qualification. No additional signing or verifier is implied.

All held rows stay literal false here. When separately authorized later, replace
false with explicit trusted repository/ref/event, readiness AND upstream-success
conditions appropriate to that job, including identity/status/build/stage results.
Never simply remove the `if`: the harmless constant-condition lint suggestion is
not activation authority. These held bodies/needs wiring remain hosted-unproven.

## Queue and summaries

Publication serializes per repository with cancel-in-progress:false. GitHub's
single pending slot intentionally replaces an older pending publication; newer
trusted run ordinal/attempt and strict SemVer freshness still apply. That is not
a promise that every prepared run publishes or that failed-job-only reruns work.
Use a NEW run for a fixed commit; an otherwise authorized same-source retry must
re-run all jobs to bind its new attempt consistently.

Named check/status/conclusion and finite refusal reasons go in bounded step
summaries with API/check text escaped as data. The existing nightly lead/QA monitor
can consume them later. No extra scheduler, aggregate watcher/heartbeat, workflow
admin, activation, new health service or egress infrastructure is introduced. The
60-day schedule-disable behavior is an operator consideration, not proof that a
schedule is active or healthy. Provenance remains neither signing nor quality or
native qualification; no current consumer attestation-verification claim is made.

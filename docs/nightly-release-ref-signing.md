# Windows release-ref nightly signer source

This is a separate source implementation, not activation or release qualification.
The existing stable `release.yml`, v1/v2 policy parsers, signing adapter and public
pruning behavior are unchanged. No production policy is installed by this repository.
PhotoCraft's prepared callers remain literally disabled until reviewed promotion.

## Trust boundaries

- `nightly-unsigned.yml` is reusable only. A credentialless main compiler preserves
  the existing Git-dependency guard, builds `package.ps1 -Nightly -BuildOnly`, and
  smokes only in that compiler job. A fresh Ubuntu attester has no app checkout,
  platform secrets, payload execution or cache. It recomputes the exact source/run
  identity and hashes the payload against the independent compiler-job output.
- `nightly-release.yml` is reusable only and serializes the entire PhotoCraft signer
  with a constant group, `queue: max`, and cancellation off. Actual official repo,
  branch `release`, and `workflow_dispatch` context are mandatory in every job.
  The app caller's concurrency group must remain distinct from the callee's group.
- A fresh Windows validator snapshots candidate quality before reading artifact
  metadata, then downloads only the authenticated producer run/attempt/artifact.
  Trusted patched `gh attestation verify` must exit successfully; its private new
  stdout file is parsed, never downloaded verification JSON or a boolean claim.
- A fresh Windows signer alone uses the existing `release` environment. It checks
  admission before the sole leaf step exports the existing PFX/Azure values. The
  protected release checkout supplies `sign.ps1`; shared existing ZIP/PE/inventory,
  mandatory configuration, adapter and native verification helpers handle data.
  No payload or main compiler/package script is executed beside keys.
- A fresh Windows publisher has no release environment/platform keys. It checks
  the independently hashed signed handoff, revalidates admission and native
  signatures, attests the actual final bytes, and exclusively creates a new nightly
  tag/draft/release. It never overwrites, deletes, prunes or repairs an existing tag.
  Confirmed public state followed by receipt/fsync/close failure is explicitly
  unreconciled, not permission to recreate or retry. Ownership evidence is retained.

Shared tooling must match the actual immutable defining workflow revision. A small
fixed-purpose Git reader verifies actual checkout HEADs and reads the policy from
the committed protected release blob, not mutable/untracked worktree JSON. Its
Git process inherits no signing credentials or external Git configuration. The
legacy native command allowlist is not widened. Independent job outputs bind
finite metadata inventories and payload bytes across fresh runners. Transport ZIP,
unsigned package and final signed ZIP digests have distinct fields and roles.

## Candidate contract and deliberate limits

`craft-nightly-candidate-policy/v3` requires exact-C latest applicable required
workflows/jobs/check identities and fixed reviewed debt fingerprints. C may be
behind current main M only with verified ancestry, and must strictly advance the
previous authenticated publication P; equal P/C refuses as already up to date.
The release execution's own SHA is never substituted with C.

Sonar admission requires coherent latest main-C revision/SCM at read boundaries,
complete paginated scoped branch issues with counts and end reread, a same-analysis
gate and exact debt. Profiles, rules, effective scanner configuration and warnings
are trusted as resolved: configuration changes can hide issues. This is not a
scanner inventory or warning-free proof. An explicitly unrelated PR CE task may
be ignored; ambiguous/failed applicable main CE state and races refuse.

Vanished debt needs both automatic analysis-driven CLOSED/FIXED lifecycle evidence
and independently qualified exact-C source-fingerprint absence. Real normalization
is not qualified here, so disappearance currently refuses; it is not treated as a
fix. Retained snapshots are fixed to C, but current implementation conservatively
refuses once the latest Sonar analysis advances beyond C because historical issue
search is unqualified. Snapshotting early reduces, but does not solve, starvation.
Unmeasured coverage is not passing coverage. Main CI/application code and resolved
mutable CI dependencies remain trusted, not a frozen test implementation.

## Validation and activation separation

`nightly-release-no-key.yml` only checks out source and runs synthetic Python unit
suites. It has contents-read only, no environment, secrets, OIDC, attestation writes,
workflow calls, producer compilation or native signing. It can run on the task
source branch/PR without invoking the release workflow or any release environment.
It is not a live provider/cryptographic/native contract qualification.

Local checks: `python3 -m unittest discover -s tests -p 'test_nightly*.py' -v` and
`python3 -m unittest discover -s tests -p test_publish_nightly.py -v`.
Static tests check the workflow boundaries; actionlint is a separate check.
actionlint 1.7.12 predates GitHub's April 2026 `job.workflow_repository/workflow_sha`
and May 2026 `concurrency.queue` features. Local lint excludes only those exact
outdated-schema diagnostics; shellcheck and pyflakes are not run. Current syntax
references are [job context](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#job-context)
and [larger queues](https://github.blog/changelog/2026-05-07-github-actions-concurrency-groups-now-allow-larger-queues/).
Hosted validation, including actual queue retention/displacement, remains separate.

Before activation: independently install real reviewed release/candidate policy;
qualify provider completeness/lifecycle/hash behavior, exact hosted attestation and
rerun/actor/substitution semantics, gh format/version and native tools; review the
entire environment-reaching executable closure against previous trusted release;
qualify actual existing ref/protection/approval behavior and independent Windows
signature/install/launch/isolation/update/uninstall/rejection acceptance. Production
signing/publishing and pruning remain off. No Windows result supplies Mac/Linux
coverage. No source CI result supplies these missing acceptance results.

Promote identical signer-owned files/policy/adapter only in an already-intended,
reviewed stable release sync. Release pushes can start/update stable drafts or
refuse published versions. An urgent repair instead needs one explicitly authorized
maintenance push with those consequences understood; no dummy bump or CI bypass.
Pausing must work through existing workflow controls without a release push. This
source change authorizes no release push/build/dispatch/signing/activation/settings
or secrets changes. Shared signing identity incidents can affect stable trust too;
reconcile the shared identity/credentials and affected outputs, not just one nightly.

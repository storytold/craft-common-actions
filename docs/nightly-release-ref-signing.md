# Windows release-ref nightly signer source

This is a separate source implementation, not activation or release qualification.
The existing stable `release.yml`, legacy candidate v1/v2 policy parsers, signing adapter and public
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
  Live admission requires explicit role-scoped release policy v2. Reviewed
  `manual_actors` grant manual producer/signer/rerun authority; a separate
  `automatic_signer_actors` list grants only the first signer attempt for an
  authenticated first scheduled producer. Neither list implicitly trusts bots.
  No real actor allowlist is installed here.
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
  Only after the packet publisher returns through every guard, asset, timestamp
  and final-ref check can a local receipt I/O failure become a warning. Callback
  confirmation of a public PATCH alone is insufficient: uncertain publication,
  post-public verification failures and non-I/O bugs remain fatal. A valid v2
  ownership receipt is written to `ownership.pending.json`, flushed, fsynced and
  closed before atomic promotion to `ownership.json`. Failed write, flush, fsync,
  close or promotion supplies no canonical ownership or pruning authority.
  The workflow uploads only the canonical filename; a missing receipt warns.
  Only this finalized-ownership upload step tolerates artifact-service failures,
  so bookkeeping cannot fail an otherwise verified publication. Every publish
  step has its own time bound and the job budget exceeds their sum, so a slow
  upload times out as a tolerated step instead of cancelling the job. Verification and
  publication steps remain fatal. Final `GITHUB_OUTPUT`
  I/O is likewise bookkeeping only after that verified return. This lets the
  hosted execution finish successfully and the unchanged success-only historical
  checks authenticate the nightly on a later run, even if its local receipt is
  missing. No pending file becomes trusted history. Existing failed runs or runner
  loss remain manual, release-manager-reviewed recovery; restoring a local receipt
  cannot change hosted conclusions or authorize recreating an existing release.

Shared tooling must match the actual immutable defining workflow revision. A small
fixed-purpose Git reader verifies actual checkout HEADs and reads the policy from
the committed protected release blob, not mutable/untracked worktree JSON. Its
Git process inherits no signing credentials or external Git configuration. The
legacy native command allowlist is not widened. Bridge outputs are confined to
the job workspace or runner temporary directory; traversal, internal symlinks,
unexpected metadata keys and unsafe asset basenames refuse before writes. Git
blob IDs retain Git's SHA-1 protocol format explicitly as compatibility hashing,
not as an artifact/signature primitive; artifact digests remain SHA-256.
Independent job outputs bind
finite metadata inventories and payload bytes across fresh runners. Transport ZIP,
unsigned package and final signed ZIP digests have distinct fields and roles.

## Role-scoped actor policy

`craft-nightly-release-policy/v2` requires both bounded, case-normalized actor
lists; normalized overlaps refuse. Reviewed `manual_actors` intentionally grant
both producer and signer manual authority. An automatic-only identity must never
be unioned into that list or the unsigned caller's `manual-actors` input.

| Authenticated role/event | Required authority |
|---|---|
| Producer `schedule`, attempt 1 | Valid current actor; existing schedule exemption |
| Producer manual dispatch or any producer rerun | `manual_actors` only |
| Signer dispatch, attempt 1, for a first scheduled producer | `manual_actors` or `automatic_signer_actors` |
| Signer attempt 1 for a manual/rerun producer, or any signer rerun | `manual_actors` only |

The validator authenticates the producer before applying an automatic signer
grant, and retained requests enforce the same rule. Missing producer proof denies
an automatic-only actor. A bot login does not identify which editable main
workflow transported the request; every independently inadmissible request still
refuses. Original initiators never substitute for current rerun actors.

Old successful history keeps its exact committed `craft-nightly-release-policy/v1`
fieldset, policy digest and reviewed broad manual-list semantics. It does not gain
an automatic list. Live v1 refuses; neither a missing field nor an old schema
silently selects v2. Request, signing and publication record versions do not change:
their policy digests already bind the explicit policy version and lists.

Before activation, Photo's non-authorizing policy example must declare v2 with
`manual_actors: []` and `automatic_signer_actors: []`, leaving every unresolved real
value null and every prepared caller literally off. A real dispatcher identity
requires separate explicit review; this source installs none. No workflow input,
token/secret scope, release environment or schedule is changed by this split.

## Candidate contract and deliberate limits

`craft-nightly-candidate-policy/v3` requires exact-C latest applicable required
workflows/jobs/check identities and fixed reviewed debt fingerprints. C may be
behind current main M only with verified ancestry, and must strictly advance the
previous authenticated publication P; equal P/C refuses as already up to date.
The release execution's own SHA is never substituted with C. A required-workflow
shape whose fewest possible revalidation reads exceed the 640-read candidate
budget refuses at contract time, before any provider read. Workflow-run `path`
values may use GitHub's documented `path@branch` spelling only for the run's own
branch; any other suffix refuses, and receipts and requests keep the bare path.

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

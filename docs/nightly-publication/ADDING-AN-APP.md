# Add nightlies to your app

This is a rollout checklist for an app lead, not a switch that enables every
Craft app. The current source is an explicitly disabled PhotoCraft Windows pilot.
Other registry entries have no admitted targets; shared workflow guards and
platform adapters are still PhotoCraft-specific. Linux/macOS paths are authored
but not qualified. Do not copy the example and claim an app is supported.

## 1. Add the app-owned nightly flavor

Keep stable and nightly installations and data separate. The app owns:

- A distinct nightly name/icon and independent package/application identity.
  PhotoCraft uses a deeper-colour background with the foreground unchanged;
  do not add a badge, crescent or N overlay to reproduce a superseded convention.
- An explicit nightly profile selected before any application data is opened;
  never silently fall back to stable preferences, saved state or document caches.
- No stable file associations or default-app takeover. Turning discovery off must
  not remove an installed nightly or its data; uninstall must preserve its profile.
- An isolated build/package adapter that does not change the stable release route.

PhotoCraft's Windows example uses `packaging/windows/package.ps1 -Arch x64
-Nightly -BuildOnly` (one command). `-Nightly` selects the app-owned flavor;
`-BuildOnly` keeps credentials out of the build. Other apps must implement and
validate their equivalent; those flags are not a universal SDK interface.

## 2. Add the shared producer target and real policy

Work with the shared workflow owner to update the app's row in
[`nightly-apps.json`](../../nightly-apps.json): exact repository/default branch,
target OS/architecture/package format, asset naming and isolation identity/profile
contract. Retain empty targets for platforms not yet qualified. A registry row
alone does not bypass the PhotoCraft-specific workflow/validator guards: adapt
and review those boundaries for the new app rather than broadening all guards.

Prepare app-owned `.github/nightly-publication-policy.json` and, before retention,
`.github/nightly-pruning-policy.json` with actual numeric repository/workflow/bot
identities and reviewed historical tooling pins. Follow
[`BASELINE-CI-POLICY.md`](../../BASELINE-CI-POLICY.md) for fresh authenticated main
checks and the independently reviewed repository digest. Do not copy PhotoCraft's
Sonar URL/check IDs or treat skipped/unknown checks as successful coverage.

## 3. Add a pinned, initially disabled caller

Use [`examples/photocraft-nightly.yml`](../../examples/photocraft-nightly.yml) as a
recipe outside the active workflow directory. In the app repo, prepare a caller
for the shared nightly workflow with the app's real name/title and the same
**published, independently reviewed immutable tooling SHA** in `uses:`,
`tooling-revision`, and the app publication policy's `tooling_revision`. For a v2
policy, its canonical digest must also match the independently reviewed
`NIGHTLY_REVIEWED_POLICY_SHA256` repository variable before a run; follow the
baseline policy's review/bootstrap procedure, not a candidate-computed trust root.
Private commits and placeholder pins are not deployable.
Keep the caller job's literal `if: ${{ false }}` guard and inert `on: {}` while
drafting; do not introduce a cron. The recommended caller is **schedule-only**,
without `workflow_dispatch`. Only at separately approved first-run activation,
replace `on: {}` with the app's approved `schedule` **and remove the caller job's
literal `if: ${{ false }}` guard**. Changing only the trigger leaves the job skipped.
This enables the caller, not the shared attestation/publication jobs; distinguish
staging-only and live-publication checks as described in section 5 below.
Automatic nightlies are opt-in, not a suite-wide switch. Measure the app's
whole-CI duration first and leave enough time between scheduled runs.

Production signing is a second, separate opt-in. The shared workflows take a
boolean `production-signing` input that defaults to `false`; while it is off a
scheduled run still does the identity and quality preflight, but no job that
uses the `nightly-signing` environment runs, nothing is signed and nothing is
staged for publication. A caller existing, or gaining a schedule, is therefore
not consent to sign. Set `production-signing: true` in the caller only with the
explicit signing and publication approval described below.

The intended signing/publication owners are the existing release-managers group
and additional people Brandon authorizes. The proposed `nightly-signing`
environment has **no per-run reviewers**, so an admitted scheduled first attempt
would run automatically. **Signing setup and activation are currently held.**
The protected-path review rule was declined to preserve CI-repair access. Without
that rule, any repository writer can edit a caller/signer or add another workflow
using the environment; the actor allowlist below is only an **accidental-use
guard, not a security boundary** or an enforcement of team-only control. This
source proposal does not resolve that trust gap, create an environment, change
permissions, or authorize provisioning/activation.

If an app separately opts into `workflow_dispatch`, set `manual-actors` to a
reviewed **literal JSON array of authorized GitHub logins in the
caller source**, for example `'["authorized-login"]'` after verifying that person's
actual authority. Do not use that example login, accept the array from dispatch
inputs, derive it from `github.actor`, or treat repository write access as
signing authority. Keep the default `'[]'` unless manual control is intended;
it admits no manual runs or re-runs. Brandon's team changes do not automatically
update this snapshot: review the literal list when authorized membership changes.
This simplest allowlist route needs no membership token or broader token scope.

Every manual dispatch and **every re-run, including a scheduled re-run**, checks
`github.triggering_actor`, not the actor who originally started the run. Unknown
or malformed context/allowlists fail closed. Each signer and the publication
path repeats admission before credential exports or mutation, so rerunning a
failed signing/publication job cannot reuse the original actor's admission.
A first scheduled attempt needs no allowlist membership or per-run approval.
Keep the same literal list throughout the caller; the producer forwards it to
native callees. Do not enable a manual caller until this authority is reviewed.

## 4. Provision signing and a Launcher capability

The repository admin/credential owner follows
[`SIGNING-ENVIRONMENT-ADMIN-STEPS.md`](SIGNING-ENVIRONMENT-ADMIN-STEPS.md).
Build without credentials, then sign/verify in the isolated trusted signing job.
Before any separately authorized activation, review all credential-bearing code
and its transitive helpers/installers, not
just the workflow filename: Windows invokes `packaging/windows/sign.ps1`, and
the pending Mac route invokes `packaging/macos/nightly-sign.sh` with its import
helper. Those are trusted app code, never downloaded-payload executables. Audit
same-name repo/org signing secrets before activation; a settings-plan review
alone does not establish credential isolation or native signing acceptance.
Public trust anchors are independently reviewed source configuration: Windows
full certificate subject after native trust, Linux public key/full fingerprint,
and Apple's team for signed/notarized Mac packages. Release downloads do not
establish those anchors; missing configuration stays unavailable, never unsigned.

In the proposed Launcher PR (not current main), the entrypoint is
`crates/native/src/nightly_profile.rs`: private `Capability`/`CAPABILITIES`,
`adapter(app, release, artifact, platform)` for admission and `for_app(app,
platform)` for retained-copy management. Rows bind platform, OS/architecture/
format/asset suffix, catalog app/repository, package identity, profile/version
and a concrete `Adapter`; the catalog entry must be Official and associations
must be `none`. Strict descriptor types are in `crates/core/src/nightly.rs`.

Ask the Launcher owner to implement/review a real app adapter too: current
executable/bundle/layout handling and asset prefix are PhotoCraft-specific.
Descriptor data must not supply commands, profile flags, paths or trust roots.
Validate the actual early profile bootstrap contract, archive/path safety,
rollback/preservation and independently delivered signing trust. An asset name,
registry row or successful build does not admit a Launcher-installable target.
Link the primary Launcher PR once it exists; do not invent a public PR link.

## 5. Qualify the real install path, then arrange activation

App lead + Launcher owner + applicable QA check the actual native package:
install beside stable, launch the correct app/version, verify signatures before
installation, confirm independent data/identity, disable discovery without losing
the installed copy, show its files, and safely uninstall while preserving its
profile and stable files. Exercise corrupt/unsigned/wrong-publisher packages and
unsupported platforms: all must fail closed. Offline mocks or a portable ZIP
alone are not native/install/signing acceptance.

Keep all unqualified platforms off. Before retention can run, the authenticated
cross-attempt recovery path and full CLI/checkpoint interruption regression must
land and be reviewed; see [`nightly-pruning-draft.md`](../nightly-pruning-draft.md).
The current preview does not supply that recovery route.

Finally present exact app/shared/Launcher changes, useful validation and remaining
settings to the user for each push/new PR. The publisher owns public writes and
the separately approved first-run timing. Keep stable releases unchanged;
activate only the admitted target(s) and the app's reviewed cadence, removing
the caller job's literal false guard as described above. For a schedule-only
caller, wait for its **first approved scheduled run**, then check the outcome
appropriate to the separately approved scope:

- **Staging only:** once its other prerequisites are qualified, expect the
  `nightly-packet-<run-id>-<run-attempt>` Actions artifact. The shared
  `.github/workflows/nightly.yml` still hard-disables `attest-nightly` and
  `publish-nightly`; caller activation alone cannot create a public nightly.
  Verify the staged packet is available, not publication or Launcher installation.
- **Live publication:** only after a separately reviewed and approved shared
  workflow change admits attestation/publication. If staging used SHA A and that
  approved change produces SHA B, publish/review B first, then update **all three
  bindings together**: caller `uses: ...@B`, caller `tooling-revision: B`, and
  `.github/nightly-publication-policy.json`'s `tooling_revision: B`. Keep the caller
  disabled while preparing this transition. For v2, independently review the
  complete updated policy/fresh baseline, recompute its canonical digest and have
  the authorized admin update `NIGHTLY_REVIEWED_POLICY_SHA256` through the
  separately approved setup procedure before starting the new producer. Merely
  calculating a digest from candidate JSON is not approval.
  Start a **fresh B-bound producer run** with its own identity/version/artifacts;
  do not reuse or relabel A's staged packet. Both bootstrap and later validation
  require agreement with the actual defining shared-workflow revision. The current
  literal-false `attest-nightly`/`publish-nightly` gates must be addressed by the
  approved shared change, not just the caller guard. Complete native/signing/quality
  prerequisites before expecting and verifying the real publication/install result.
  Before any later retention activation, review the pruning policy's historical
  tooling allowlist for B too, retaining pins needed to authenticate existing owned
  releases. This guide performs none of these source/settings changes and grants
  no activation authority.

The post-publication prune-plan step is independently hard-disabled. It is not
needed to create a release and must not be enabled just to make publication
verification pass. Any separately approved retention activation must also address
that step after the recovery/quota prerequisites above; keep deletion off otherwise.
First-run verification is not a prerequisite for giving the caller its first
trigger. A manual first run is optional and needs a
separately approved narrow `workflow_dispatch` route and the reviewed actor
allowlist above, not a broad dispatch button. This guide grants no public or live
activation authority.

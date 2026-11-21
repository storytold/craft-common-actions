# Add nightlies to your app

This is a rollout checklist for an app lead, not a switch that enables every
Craft app. The current source is an explicitly disabled PhotoCraft Windows pilot.
Other registry entries have no admitted targets; shared workflow guards and
platform adapters are still PhotoCraft-specific. Linux/macOS paths are authored
but not qualified. Do not copy the example and claim an app is supported.

## 1. Add the app-owned nightly flavor

Keep stable and nightly installations and data separate. The app owns:

- A visibly badged nightly name/icon and independent package/application identity.
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
**published, independently reviewed immutable tooling SHA** in both `uses:` and
`tooling-revision`. Private commits and placeholder pins are not deployable.
Keep the caller's first-run guard off; do not introduce a cron while drafting.
Choose a schedule only after measuring the app's whole-CI duration.

## 4. Provision signing and a Launcher capability

The repository admin/credential owner follows
[`SIGNING-ENVIRONMENT-ADMIN-STEPS.md`](SIGNING-ENVIRONMENT-ADMIN-STEPS.md).
Build without credentials, then sign/verify in the isolated trusted signing job.
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
the separately approved first manual timing. Keep stable releases unchanged;
start only the admitted target(s), verify the real publication/install result,
then consider a schedule. This guide grants no public or live activation authority.

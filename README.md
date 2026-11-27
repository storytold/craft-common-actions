# artcraft-common-actions

<a href="https://sonarcloud.io/project/overview?id=storytold_craft-common-actions"><img alt="Scanned by SonarCloud" src="https://img.shields.io/badge/Scanned_by-SonarCloud-4b9fd5?style=flat-square&logo=sonarqubecloud&logoColor=white"></a>

Reusable GitHub Actions workflows and composite actions for Storyteller / ArtCraft desktop and web applications.

This repository unifies and eliminates duplicate CI/CD and release workflow definitions across the ArtCraft ecosystem into DRY, reusable GitHub Actions.

## Workflows Included

### 1. `release.yml`
Full multi-platform release pipeline for Craft apps:
- **macOS universal** (x86_64 + aarch64 universal bundle, DMG, CLI, Developer ID codesign & notarization)
- **Windows** (x64, ARM64 MSI installer with WiX v5, CLI, Azure Trusted Signing)
- **Linux** (x86_64, aarch64 AppImage, deb, rpm, tar.gz via nfpm)
- **Flatpak** (x86_64, aarch64 bundle generation)
- **FreeBSD** (FreeBSD 15.1-STABLE VM build & packaging for x86_64 and aarch64; aarch64 is cross-compiled in the same x86_64 VM and set by default through `freebsd-architectures`, see #39)
- **Arch Linux** (x86_64, aarch64 `.pkg.tar.zst` built via `makepkg` from release tarball, named by bundle id; set `has-arch: false` to skip)
- **WebAssembly** (wasm32-unknown-unknown with Trunk)
- **Linux RISC-V 64** (cross-compilation for riscv64gc-unknown-linux-gnu, producing deb, rpm, and tar.gz via SHA256-verified nfpm; built on Ubuntu 24.04, so it sets `GLIBC_FLOOR=2.39` for the app's `nfpm.yaml` to declare, see #44)
- **Draft Release** (signs the Linux packages, computes and signs `SHA256SUMS.txt`, and publishes/updates draft GitHub Release; see [Linux package signing](#linux-package-signing))

Every build job sets `VERSION`, `BUILD_DATE` and `BUILD_SHA`, and the app's own
`<PREFIX>_VERSION`, `<PREFIX>_BUILD_DATE` and `<PREFIX>_BUILD_SHA`. The prefix is the one the app's
`packaging/env.sh` (or `packaging/common.sh`) reads in `VERSION="${<PREFIX>_VERSION:-...}"`, so a
new app needs no change here; without that line it is `app-name` in capitals (`craft-anything`:
`CRAFT_ANYTHING`).

#### Cutting a release

`scripts/cut_release.py <owner/app>` cuts a release without a version-bump pull request, so a
change of version strings never waits on the full CI of a pull request:

1. It takes the newest commit on main whose `CI` run for a push passed (`--commit` picks another;
   `--gate-workflow` names another workflow).
2. It commits the version bump there: `[workspace.package] version`, the internal path
   dependencies, and Cargo.lock's entries for the workspace's own packages. A repository with
   other files that name the version adds `packaging/set-version.sh <old> <new>`.
3. It merges that into `release` (main's version lines win) and pushes, which starts the app's
   `release.yml`.
4. It opens a pull request that brings the same bump to main, with auto-merge on where the
   repository allows it. The release doesn't wait for it.
5. With `--publish` it waits for `release.yml` and publishes the draft once every job passed and
   the draft has assets. Otherwise it stops after the push.

The version defaults to the next minor after the last published release (`--part patch`, or
`--version 1.2.3-rc.1`). `--dry-run` does everything but push. It needs git, cargo (to check
Cargo.lock) and an authenticated `gh` whose account may update `release`.

`scripts/release_train.py --all --skip figcraft,soundcraft --publish` runs it for many apps at once, in
parallel, with a log per app and a summary of what was published or stopped.

#### Caller Example:
```yaml
name: Release

on:
  push:
    branches: [release]
  workflow_dispatch:
    inputs:
      version:
        description: "Version override for a test run, e.g. 0.2.0-rc.1 (empty: Cargo.toml)"
        type: string
        default: ""

permissions:
  contents: write
  packages: write

concurrency:
  group: release-${{ github.ref }}
  cancel-in-progress: false

jobs:
  release:
    uses: storytold/craft-common-actions/.github/workflows/release.yml@main
    with:
      app-name: photocraft
      app-title: PhotoCraft
      version: ${{ inputs.version }}
    secrets:
      APPLE_CERTIFICATE: ${{ secrets.APPLE_CERTIFICATE }}
      APPLE_CERTIFICATE_PASSWORD: ${{ secrets.APPLE_CERTIFICATE_PASSWORD }}
      KEYCHAIN_PASSWORD: ${{ secrets.KEYCHAIN_PASSWORD }}
      APPLE_ID: ${{ secrets.APPLE_ID }}
      APPLE_PASSWORD: ${{ secrets.APPLE_PASSWORD }}
      APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}
      WINDOWS_CERTIFICATE: ${{ secrets.WINDOWS_CERTIFICATE }}
      WINDOWS_CERTIFICATE_PASSWORD: ${{ secrets.WINDOWS_CERTIFICATE_PASSWORD }}
      AZURE_TENANT_ID: ${{ secrets.AZURE_TENANT_ID }}
      AZURE_CLIENT_ID: ${{ secrets.AZURE_CLIENT_ID }}
      AZURE_CLIENT_SECRET: ${{ secrets.AZURE_CLIENT_SECRET }}
      AZURE_SIGNING_ENDPOINT: ${{ secrets.AZURE_SIGNING_ENDPOINT }}
      AZURE_SIGNING_ACCOUNT: ${{ secrets.AZURE_SIGNING_ACCOUNT }}
      AZURE_CERT_PROFILE: ${{ secrets.AZURE_CERT_PROFILE }}
      LINUX_SIGNING_KEY: ${{ secrets.LINUX_SIGNING_KEY }}
```

#### Linux package signing

With `LINUX_SIGNING_KEY` set in the repo's `release` environment and listed in the caller's
`secrets:` block, a release signs every Linux format with that OpenPGP key:

| Format | Signature | Checked by |
|---|---|---|
| `.rpm` | embedded (`rpmsign`) | `rpm -K`, dnf/zypper with `gpgcheck` |
| `.deb` | embedded `_gpgorigin` member (`debsigs`) and `<file>.asc` | `debsig-verify`, `gpg --verify` |
| `.pkg.tar.zst` | `<file>.sig` | pacman, `pacman-key --verify` |
| `.AppImage`, `.tar.gz`, `.zsync` | `<file>.asc` | `gpg --verify` |
| `.flatpak` | signed commit, public key embedded in the bundle, and `<file>.asc` | `flatpak install` checks the embedded key; authenticate it separately on first install |
| Flatpak OCI image | `<file>.oci.sig` for `ghcr.io/<repo>@<digest>` (manifest in `<file>.oci-manifest.json`) | Flatpak, from the remote's signature lookaside |
| everything in the release | `SHA256SUMS.txt.asc` | `gpg --verify SHA256SUMS.txt.asc` |

The release also carries the public key as `release-signing-key.asc`; publish its fingerprint
somewhere users can check it independently. On first install, compare that fingerprint with the key in the bundle: a bundle can contain an attacker-controlled key. Signed bundles omit the OCI update URL until the signature lookaside is deployed. Once it is live and the launcher configures it, set the `LINUX_OCI_SIGNATURES_READY` release environment variable to `1` in each app; subsequent stable bundles then include the OCI URL. Earlier bundles can be updated by installing a newer bundle. Signing is limited to builds from the `release` branch. The Flatpak bundle is signed when the app's
`packaging/linux/flatpak-bundle.sh` has been updated with `scripts/scaffold-flatpak-oci.sh`.

The workflow pushes the Flatpak OCI image itself: the app's `packaging/linux/flatpak-oci.sh` only
builds the layout. Every run except a pull request pushes `ghcr.io/<repo>:<version>-<arch>`; only a
stable version built from the `release` branch also moves `stable-<arch>`, whatever the app's copy
of the script does with `--push`. When `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` are configured
(e.g. as organization secrets), the job dual-pushes the OCI layout to `docker.io/storytold/<app>`
with the same version and stable architecture tags.

The key must sign without a passphrase. Keep the primary key offline and store only a signing
subkey:

```sh
gpg --quick-add-key <primary-fingerprint> ed25519 sign 2y
gpg --armor --export-secret-subkeys <signing-subkey-fingerprint>! | gh secret set LINUX_SIGNING_KEY --env release
```

Without the secret the release is published unsigned, with a warning.

#### Automatic contributor credits (opt-in)

Apps using `contributors/contributors.json` and `contributors/people.toml` can refresh bundled
credits once before a release. The generator is supplied by this repository; apps do not need
their own refresh script or an application runtime dependency on Python.
Grant the caller workflow `contents: write` for release publishing and `packages: write` for
the shared packaging jobs, and keep the preparatory
`credits` job read-only with its job-level override. A called workflow cannot increase the
token permissions granted by its caller; see [reusable-workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#supported-keywords-for-jobs-that-call-a-reusable-workflow).
Call the preparatory workflow, then pass its output to the release workflow:

```yaml
permissions:
  contents: write
  packages: write

jobs:
  credits:
    permissions:
      contents: read
      pull-requests: read
    uses: storytold/craft-common-actions/.github/workflows/refresh-contributors.yml@main
  release:
    needs: credits
    permissions:
      contents: write
      packages: write
    uses: storytold/craft-common-actions/.github/workflows/release.yml@main
    with:
      app-name: effectcraft
      app-title: EffectCraft
      contributors-artifact: ${{ needs.credits.outputs.artifact-name }}
    # Include the app's existing signing secrets here.
```

The preparatory workflow checks out the app's full history and invokes
`$/actions/refresh-contributors`, so the generator comes from the same repository and commit
as the reusable workflow even when it is pinned or called from a fork. The generator runs
with `--repo` set to `github.repository` and a read-only `GH_TOKEN`, and validates that the resulting nonempty
`contributors/contributors.json` belongs to `github.sha`. It uploads that file only after a
successful refresh. The intermediate `contributor-snapshot-*` artifact stays outside the
`release-*` collection used for release assets and checksums, including on reruns.
Every source-build job installs the same artifact after checkout, including
FreeBSD before copying the workspace to its VM. Flatpak and Arch inherit the refreshed credits
from the Linux binaries. The generated snapshot is not committed or pushed back to the app.

An unsuccessful refresh or missing artifact blocks the release. Existing callers that omit
`contributors-artifact` keep their current behavior, permissions, and build paths.

The shared generator uses local Git text/binary statistics and GitHub-linked author/co-author
identities and merged PRs. A commit with no linked person is credited to the opener of the merged
PR that contains it, found through GitHub's commit-to-PR association, so squash, rebase and
merge-commit merges all count; it stays unattributed only when no single opener is known. Merge
commits count as commits.

AI agents and bots are never listed as people. GitHub links agent trailers such as
`noreply@anthropic.com` to accounts (`claude`, `codex`), and coding agents such as Copilot, Cursor or
Devin author commits themselves; both are credited in the snapshot's `models`, which apps show
under **About ▸ Models**. Agents are recognized only by their exact Git email or GitHub account
(`AGENT_EMAILS` and `AGENT_LOGINS` in the generator), never by name, so a person's work address at
an AI company is still a person. A recognized agent's model and version are read from its Git name
(`Claude Opus 5.5`, `GPT-6.1 Sol`, `DeepSeek V4.1 Flash`), using only the words those tables list for
that provider: Git names are free text that ends up in the apps, so any other word credits the
agent's default model instead. Unrecognized emails are skipped. Add a new agent or model word to
those tables. Other `[bot]` accounts, such as dependabot, are left out.

Name preferences are verified against the commits that introduced their values. A Git
email-linked author is trusted for consent only when GitHub reports a valid signature whose signer
matches that author; otherwise verification uses the authenticated opener of the merged PR that
contains the commit, however it was merged. An integration PR opened by someone with repository
write access that carries the person's own GitHub-linked commit, keeping its authorship, also
verifies it. Any other maintainer override requires a recorded `consent_source` and repository
write access. Public profile names are fetched only for verified
entries opting into synchronization. Co-authored commits count for each linked contributor and
model, so row totals can exceed repository totals. Generation replaces only the snapshot,
atomically after collection succeeds, and never edits `people.toml` or commits/pushes changes.
Name preferences and their consent checks use the captured Git commit, so concurrent local
edits cannot change the release snapshot. Uncommitted preferences are rejected before API
access with an instruction to commit or restore them, without logging their contents.

For a manual refresh, use Python 3.11+, Git and an authenticated `gh` CLI from a checkout of
this repository:

```sh
python3 actions/refresh-contributors/refresh_contributors.py --repo storytold/effectcraft --directory ../effectcraft
```

The target app checkout must contain full history and the intended release commit. Python
runs on the build machine; the app continues to embed the resulting JSON through its existing
Rust build code.

List every signing secret explicitly. The signing jobs run in the caller repo's `release` environment and read the values from there, but only for the names listed here: an unlisted name is empty. On the `release` branch the macOS and Windows jobs fail when their signing secrets are empty, rather than shipping unsigned builds.

macOS and Windows packages are signed by the app's packaging scripts, with the shared certificate import and signing script (see "macOS code signing" and "Windows code signing"). `release.yml` does not request `id-token` or `attestations`, so existing callers keep building as they do now.

Build provenance, a CycloneDX SBOM, and a Sigstore signature of the published files are a second job. Add it only in an app that is ready to grant those permissions. `release.yml` does not call `supply-chain.yml`.

```yaml
  supply-chain:
    needs: release
    if: github.ref == 'refs/heads/release'
    permissions:
      contents: write
      id-token: write
      attestations: write
      artifact-metadata: write
    uses: storytold/craft-common-actions/.github/workflows/supply-chain.yml@main
    with:
      version: ${{ needs.release.outputs.version }}
```

After a release that ran the extra job:

```sh
gh attestation verify PhotoCraft-1.2.3-macos-universal.zip --owner storytold
```

The command checks the signature and prints the repository and commit that built the file. The SBOM is attached to the draft release as `sbom.cyclonedx.json`.

`craft-fonts-ref` feeds the desktop builds. It defaults to the suite's craft-fonts pin, so one bump
here reaches every app's next release; set it only when an app needs another commit. Every desktop
build runs with `CRAFT_FONTS_DIR` and `CRAFT_FONTS_REQUIRED=1`: an app whose `build.rs` reads
craft-fonts fails the build instead of shipping without its fonts. The web build gets craft-fonts
too only with `web-craft-fonts: true`: each app's `build.rs` decides which fonts its web build
embeds (craft-fonts `docs/integration.md`), so opt in once that list fits the app's web size budget.

An app pins its craft-i18n commit in `craft-i18n.rev` at its root, one SHA (keep it LF with
`craft-i18n.rev text eol=lf` in `.gitattributes`). Its presence switches craft-i18n on: every
build job, here and in `freebsd.yml` and `windows-arm64.yml`, checks out craft-i18n at that SHA
and builds with `CRAFT_I18N_DIR` and `CRAFT_I18N_REQUIRED=1`. That fails only on a missing or
invalid craft-i18n checkout; a missing translation never blocks a build, pin bump or release
(storytold/craft-i18n#20). An app without `craft-i18n.rev` builds as before.

### 2. `packaging-lint.yml`
Lints all packaging configurations and metadata:
- `actionlint` for workflow syntax
- `shellcheck` for shell scripts and AppRun
- PowerShell parser validation for WiX helper scripts
- XML lint for WiX (`.wxs`, `.wxl`), macOS (`Info.plist`), and Linux AppStream metadata
- `desktop-file-validate`
- `appstreamcli validate`
- Flatpak manifest consistency checks
- FreeBSD dry run & embedded icons verification
- AppImage desktop integration and bundled CLI/MCP tests, when the caller provides
  `packaging/linux/apprun-test.sh` or `packaging/linux/apprun-cli-test.sh`. No app-local
  workflow job is needed; a failing script fails the shared lint job.

#### Caller Example:
```yaml
name: Packaging lint

on:
  push:
    branches: [main, release]
    paths: [".github/workflows/**", "packaging/**", "assets/app-icon/**"]
  pull_request:
    paths: [".github/workflows/**", "packaging/**", "assets/app-icon/**"]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: packaging-lint-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint:
    uses: storytold/craft-common-actions/.github/workflows/packaging-lint.yml@main
```

### 3. `windows-arm64.yml`
Cross-compiles ARM64 MSI on x64 Windows runner, then installs, verifies native ARM64 architecture (`0xAA64`), smoke tests the CLI, and uninstalls on a Windows 11 ARM64 runner.

`app-dir` stays a single display/shortcut/registration name. Set `install-relative-path`
separately for nested folders, for example `Programs/ArtCraft/PhotoCraft` below LocalAppData
with `install-scope: perUser`, or `ArtCraft/PhotoCraft` below ProgramFiles with
`install-scope: perMachine`. An empty scope retains the legacy installer defaults and
`is-per-user` behaviour. Explicit scope is passed to msiexec as well as used for path checks.
For the launcher's per-user-only installer, keep `is-per-user: true` to retain its shortcut,
registry and custom-folder checks. Path components are validated before starting msiexec.


#### Caller Example:
```yaml
name: Windows ARM64

on:
  push:
    branches: [main]
  pull_request:
    paths:
      - Cargo.toml
      - Cargo.lock
      - "**/Cargo.toml"
      - "**/build.rs"
      - .github/workflows/windows-arm64.yml
      - packaging/windows/**
  schedule:
    - cron: "41 4 * * 1"
  workflow_dispatch:

concurrency:
  group: windows-arm64-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  windows-arm64:
    uses: storytold/craft-common-actions/.github/workflows/windows-arm64.yml@main
    with:
      app-name: wordcraft
      app-dir: WordCraft
```

### 4. `freebsd.yml`
Builds and runs workspace tests inside a FreeBSD 15 VM on a Linux runner.
Pull requests default to `x86_64`; pushes, schedules and manual runs retain both
`x86_64` and `aarch64`. Both boot a KVM-accelerated x86_64 VM; `aarch64` is
cross-compiled in it with stable Rust (the standard library comes from FreeBSD's
aarch64 `lang/rust` package, against an arm64 sysroot built from `base.txz`). Its
tests are built but not run, and its package check is skipped.

The release workflow builds FreeBSD for `freebsd-architectures`, which defaults to
`'["x86_64","aarch64"]'`; callers can narrow it, for example `'["x86_64"]'`. An app's
`packaging/freebsd/package.sh` must build with `CARGO_BUILD_TARGET`, read binaries
from `$CARGO_TARGET_DIR/$CARGO_BUILD_TARGET/release` and name its archive
`*-freebsd-<arch>.tar.gz` for each listed architecture: the guest's `uname -m` is
always `amd64`. The release job fails if an archive does not match its matrix
architecture. Apps without a `package.sh` use the built-in packager, which already
handles the cross target.

The optional `architectures` input overrides the check matrix with a JSON array,
for example `'["aarch64"]'` for a dedicated ARM check or
`'["x86_64","aarch64"]'` for an explicit full check. Existing callers need no
changes. A reusable workflow cannot install a schedule in its caller repository;
add a caller `schedule` if nightly coverage is desired. If branch protection requires
the ARM job by name, remove that required check when adopting the new PR default,
or explicitly request both architectures until that policy is approved. Main/release pushes and
manual runs keep their existing full coverage.

Both this workflow and the FreeBSD jobs in `release.yml` cache Cargo registry/git
data and compiled targets through `actions/freebsd-cargo-cache`. The VM starts
first; the action fingerprints its actual Rust compiler, FreeBSD kernel/userland,
architecture and installed package versions. Inputs (including font refs and
feature/test options), manifests, lockfiles, build scripts, Cargo configuration,
toolchain files and packaging scripts decide which cache is an exact match. When
there is no exact match, the action falls back to the most recent cache with the
same toolchain, configuration and profile, so a release that only bumps a version,
or a PR that touches a manifest, still restores compiled dependencies instead of
rebuilding them cold; Cargo's own fingerprints decide which of those artifacts
are still valid. Toolchain, configuration and profile changes start a new cache
without a fallback. A run saves only when no entry for the same exact match was saved that
day (UTC), so a busy main keeps one entry per dependency fingerprint and day instead of one
per commit; later runs restore that entry and Cargo rebuilds only what changed since. The test and release workflows use separate profile
namespaces. The workflow-file pattern fingerprints the caller's configuration;
changes to the shared workflow that need a new namespace must bump the cache
action's version prefix.

Cache data is restored on the host and explicitly transferred to the guest's
`/var/tmp/craft-cargo` and `/var/tmp/craft-target`. Only registry/git data and
`debug`/`release` artifacts are saved, excluding incremental state, Cargo
credentials/config and Rust toolchains. A cross build passes its triple as `target`, and
its `target/<triple>/<profile>` output is saved and restored too: without it, the aarch64
release job kept only its build scripts and compiled everything else cold. `release.yml`
keys its cache on the inputs that change the FreeBSD build (app name, architecture,
craft-fonts ref and FreeBSD release), not on all its inputs: `contributors-artifact`
names the run, so a key that held it could never be restored, and a `dry-run` shares
the release's entry, so a dry run warms the cache a release restores. Successful non-PR runs save caches on a best-effort basis; PRs
only restore. Restore, checkpoint and save failures are non-blocking; each phase has a 10-minute
time budget. A failed
restore discards partial guest data before building; a failed checkpoint discards
the partial release profile before packaging. Required build/package steps reclaim
debug disk space independently of the optional checkpoint. GitHub's normal repository and branch cache access rules apply.
The VM's base image cache is separate from this Cargo cache.

For package checks, debug artifacts are checkpointed on the host before freeing
guest disk space and loading the cached release profile. Release jobs keep their
compiled target until the cache has been copied out, copy only the release
archives back and upload them before attempting the optional cache save. Neither workflow copies the entire VM workspace back.
Cargo cache payloads are not size-pruned locally. GitHub manages compressed cache
storage and eviction under the repository's configured limits and applicable
organization/enterprise policies. Architectures, profiles and revisions share
that quota with other workflows.
Cache storage and transfer time should be monitored against repository quotas;
the first run is cold, and no fixed speed-up is assumed.

`craft-fonts-ref` (default: the same pin as `release.yml`; empty for none) makes the checkout
available to builds, default tests and custom tests through `CRAFT_FONTS_DIR`. Set
`require-craft-fonts: true` to also export `CRAFT_FONTS_REQUIRED=1` and fail before building if
the checkout is missing.
The application's build script validates its required font faces.

Tests (default and custom) run as an ordinary user, `craft-test`, not root: root is never
refused a file, so a test that expects "permission denied" would fail. The build and packaging
stay root.

`cargo-build-extra-args` and `cargo-test-extra-args` accept whitespace-separated
arguments with shell-style quotes and escapes for values containing spaces.
Arguments are passed literally: shell commands, variable expansion and globbing
are not evaluated. Malformed quoting fails the invoked build or test step.

Use a multiline `custom-test-command` for separate test groups. It runs in a
separate `sh -eu` process whose failure stops the VM script before packaging.
Do not rely on `errexit` to catch nonfinal commands of an `&&` list followed by
other commands; write each required test command on its own line.

`run-package-check: true` requires `packaging/freebsd/package.sh` to exist and
succeed, then requires at least one readable `*-freebsd-<arch>.tar.gz` in
`dist/release` for each matrix architecture. Each archive is checked separately. The app's package script
retains responsibility for its required binaries, metadata and licence files.

#### Caller Example:
```yaml
name: FreeBSD

on:
  push:
    branches: [main]
  pull_request:
    paths:
      - Cargo.toml
      - Cargo.lock
      - "**/Cargo.toml"
      - .github/workflows/freebsd.yml
  workflow_dispatch:

concurrency:
  group: freebsd-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  freebsd:
    uses: storytold/craft-common-actions/.github/workflows/freebsd.yml@main
    with:
      require-craft-fonts: true
```

### 5. `nix.yml`

Evaluates the repository's Nix flake across supported systems without building, validating flake evaluation and `Cargo.lock` synchronization.

`extra-args` defaults to `--no-build --all-systems`. It accepts whitespace-separated
arguments with shell-style quotes and escapes for values containing spaces.
Arguments are passed literally: shell commands, variable expansion and globbing
are not evaluated. Malformed quoting fails the check.

#### Caller Example:

```yaml
name: Nix

on:
  push:
    branches: [main]
    paths: ["flake.nix", "flake.lock", "nix/**", "Cargo.lock", ".github/workflows/nix.yml"]
  pull_request:
    paths: ["flake.nix", "flake.lock", "nix/**", "Cargo.lock", ".github/workflows/nix.yml"]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: nix-${{ github.ref }}
  cancel-in-progress: true

jobs:
  nix:
    uses: storytold/craft-common-actions/.github/workflows/nix.yml@main
```

### 6. `gate.yml`

The quality gate for an app's pull requests and pushes to main. It runs `command` (by default
`cargo xtask ci`) on each runner in `os`, after the suite's shared setup:

- the app and craft-fonts (at the suite's pin, with `CRAFT_FONTS_REQUIRED=1`) checked out without
  persisted credentials, and craft-i18n when the app has `craft-i18n.rev`;
- `packages` installed through `actions/install-apt-packages` on Linux legs (none when empty);
- an optional `setup-command` run from the repository root before the gate, for fetching pinned
  test data such as licensed corpora; its environment changes can be written to `$GITHUB_ENV`;
- with `cache-paths` and `cache-key`, those paths restored before `setup-command` and saved after
  it where the dependency cache saves, as one entry for every OS. Put the pins of what the setup
  fetches in `cache-key` (`psd-corpus-<commit>`), or keep them in a file and name it in
  `cache-key-files` (its hash joins the key), and let the setup skip a fetch whose files are
  already there;
- the channel from `rust-toolchain.toml`, or `toolchain`, with clippy, rustfmt and `targets`;
- prebuilt cargo tools from `tools` (by default `cargo-nextest@0.9.150`);
- `CARGO_PROFILE_DEV_DEBUG=line-tables-only` (set `dev-debug: ""` to keep the repository's
  profile), `dev-strip: true` (no debug info and stripped symbols in the test profile, for CI test
  builds; off by default) and the caller's `env`, `linux-env` and `windows-env` lines, each `KEY=VALUE`;
- a rust-cache dependency cache saved by the default branch by default; give each call of the
  workflow in one job a `cache-name` (`gate-<name>` entry) so the calls do not share one key; Pull requests only
  restore caches; callers may opt manual branch runs into branch-scoped writes with
  `cache-save-on-dispatch: true`. The restore prefix includes the canonical root `[profile]`
  table without the wasm-only profiles (names `web` or `wasm`, with or without a `-suffix`);
  dependency, version, comment and web-profile edits do not discard reusable dependency
  artifacts.
  rust-cache still keys on lockfiles, member manifests, toolchain and build environment, and
  Cargo validates restored fingerprints. Bump `cache-version` to start afresh;
- with `sccache: true`, sccache (through the GitHub Actions cache) for the workspace's own
  library crates, which rust-cache doesn't keep. It writes where the dependency cache saves and
  only reads elsewhere; set `SCCACHE_GHA_RW_MODE` in `env` to choose. On the cluster runners,
  which set `SCCACHE_WEBDAV_ENDPOINT`, sccache uses the shared cluster cache instead, under the
  key prefix `sccache/<cache-version>` that every repository shares. Two controls keep PR runs
  from writing: gate-setup sets `SCCACHE_WEBDAV_RW_MODE=READ_ONLY` wherever the dependency cache
  doesn't save (`SCCACHE_WEBDAV_RW_MODE` in `env` overrides it), and the cache server accepts
  writes only with the writer credential, which only the main-branch runner set holds. Test, binary and
  proc-macro crates aren't cacheable, so it mainly helps workspaces with many library crates.
  sccache's cache starts afresh when `cache-version` or any `Cargo.toml` or `build.rs` changes,
  because its key leaves out `--check-cfg`: a cached compile would replay its old
  `unexpected_cfgs` warnings after the expected cfgs change. It also can't see files a procedural
  macro reads without rustc tracking them (some asset-embedding macros): don't opt in while
  such files change often, or bump `cache-version` when they do. `SCCACHE_GHA_VERSION` in
  `env` sets the cache's version instead;

With `warm-command`, a default-branch run (or an opted-in manual run) adds a `cache warm (<os>)`
leg per runner beside the gate leg. It runs only that command, for example
`cargo test --workspace --no-run --locked`, and saves the `gate` dependency entry as soon as it
succeeds. The gate legs then only restore. Without it, the gate saves at the end of a green run,
so a main whose tests fail never saves and every pull request builds cold. Pull requests never
get the leg. Give the command the build the gate does (same features, profile and targets), since
the entry holds what that build compiled; the leg has the same setup, environment and
`setup-command` as the gate. With `archive-command` the build job saves the entry and no warm leg
runs.

With `lint-command`, each runner in `os` also gets a `lint (<os>)` leg beside its `gate (<os>)`
leg. It has the same setup except `setup-command`, runs `lint-command` (fmt, clippy, other checks
that need no test build), and keeps a rust-cache entry of its own, since a check build's
dependencies are not the test build's. Clippy then runs beside the test build instead of before
it. Empty (the default) runs no lint legs, and the test legs keep their names:

```yaml
    with:
      lint-command: |
        cargo fmt --check
        cargo clippy --workspace --all-targets --locked -- -D warnings
      command: cargo nextest run --workspace --locked --profile ci
```

With `test-shards: N` (N > 0), each runner runs its tests as N legs, `test <k>/<N> (<os>)`, in
place of its `gate (<os>)` leg. The default, 0, runs one `gate (<os>)` leg per runner as described
above.

- Each shard leg has the gate leg's full setup, restores the gate's Rust cache entry, and runs
  `shard-command` (`windows-shard-command` on Windows), which builds and runs its share of the
  tests. The gate sets `GATE_SHARD` (1 to N), `GATE_SHARDS` (N) and `NEXTEST_PARTITION`
  (`hash:<GATE_SHARD>/<GATE_SHARDS>`) for it.
- Shard 1 saves the cache by the gate leg's rules; with `warm-command` the `cache warm (<os>)` leg
  saves instead. The other shards only restore.
- `lint (<os>)` and `cache warm (<os>)` still run once per runner.
- A `gate (<os>)` job passes only when every shard of that runner passed, so required checks keep
  their names. It waits for every shard leg; the shards themselves start at once.
- Each shard leg records its status in an artifact and a file named after the runner label
  (`gate-shard-<os>-<k>`), so a workflow run can hold one sharded gate call per runner label. A
  re-run replaces the status of every leg it runs.

```yaml
    with:
      test-shards: 4
      shard-command: cargo nextest run --workspace --locked --profile ci --partition "$NEXTEST_PARTITION"
```

With `archive-command` as well, each runner builds its tests once instead of once per shard:

- A `build (<os>)` job runs `archive-command`, which writes a nextest archive to
  `$NEXTEST_ARCHIVE_FILE`, and uploads it as the `gate-archive-<os>` artifact (uncompressed: the
  archive is already zstd). Where the dependency cache saves, this job saves the `gate` entry.
- The shard legs wait for it, download the archive, restore no cache and build nothing:
  `shard-command` runs the tests from `$NEXTEST_ARCHIVE_FILE`
  (`cargo nextest run --archive-file "$NEXTEST_ARCHIVE_FILE" ...`). The lint legs never wait for
  the build job.
- `after-archive-command` runs in the build job after the upload, for checks that need the build
  directory, such as doctests. The shards wait for the whole build job, its cache save included,
  so this command's time adds to the gate's wall time. `gate (<os>)` passes only when the build
  job passed too.
- `test-shards` must be set: the shards are what run the archive.
- A failed build skips the shards of every runner in the call. Give each runner its own call to
  keep one runner's build failure from hiding another runner's test results.

```yaml
    with:
      test-shards: 4
      archive-command: cargo nextest archive --workspace --locked --archive-file "$NEXTEST_ARCHIVE_FILE"
      after-archive-command: cargo test --doc --workspace --locked
      shard-command: cargo nextest run --archive-file "$NEXTEST_ARCHIVE_FILE" --workspace-remap . --partition "$NEXTEST_PARTITION"
```

With `shard-archives: true` as well, each shard downloads only the test binaries it runs:

- The build job sets `$NEXTEST_ARCHIVE_DIR`, `$NEXTEST_ARCHIVE_NAME` (the `archive-file` input)
  and `$GATE_SHARDS` instead of `$NEXTEST_ARCHIVE_FILE`. `archive-command` writes shard k's archive
  to `$NEXTEST_ARCHIVE_DIR/<k>/$NEXTEST_ARCHIVE_NAME` for every k from 1 to `$GATE_SHARDS`, for
  example with `cargo nextest archive -E 'binary_id(=...) | ...'` per shard, and the build job
  fails if one is missing.
- Each archive is uploaded as `gate-archive-<os>-<k>`, and shard k downloads only that one. On
  the shard, `$NEXTEST_ARCHIVE_FILE` points at it as before.
- Which tests each shard runs is the caller's to decide and to record: a shard runs its archive
  without a partition. PhotoCraft balances the shards by recorded test times, splits binaries too
  heavy for one shard by test filter, and puts its plan into every archive (its
  `xtask/src/shards.rs`).
- At most 8 shards: the build job has one upload step per archive, since an upload step takes one
  artifact name. The select job rejects more.

```yaml
    with:
      test-shards: 4
      archive-command: cargo xtask ci archive
      shard-archives: true
      shard-command: bash scripts/ci-test-shard.sh
```

With `cache-workspace-crates: true`, the dependency entry also keeps the workspace crates'
compiled libraries and unit-test executables, so a change rebuilds only the crates it reaches:

- The entry rolls: the saving leg (the build job, else the warm leg or shard 1) writes an entry of
  its own each run, and every leg restores the newest one by its prefix. A fixed key would never be
  replaced, so its commit would fall ever further behind.
- A run's entry is named after the entry it restored (`<job>-crates-<os>-<arch>-<parent run id>-0`,
  from `.ci-cache-meta/entry`), so when overlapping runs restored the same entry and have the same
  lockfile and toolchain, only the first to finish saves: GitHub refuses a key that exists, and the
  later run logs one "Cache reservation failed" warning. Nothing restored gives `-0-0`; a cold build writes `-<run id>-<attempt>`.
- The saved entry holds only the build units the run's build used, when the build records them
  (Cargo's JSON messages in the file `CRAFT_CACHE_UNITS` names; see `actions/cache-trim`). Without
  a record, rust-cache trims by package name, which keeps older builds of every package still in the
  lockfile: after a dependency update the entry grows by the old builds.
- The caller keeps one entry per family with `actions/cache-prune` in a job of its own that holds
  `actions: write` (gate.yml's jobs hold no write permission). The step runs with
  `continue-on-error`, so a failed prune warns instead of failing the run:

```yaml
  cache-prune:
    name: cache prune
    if: github.event_name != 'pull_request' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-24.04
    timeout-minutes: 120
    permissions:
      actions: write
    steps:
      - uses: storytold/craft-common-actions/actions/cache-prune@main
        continue-on-error: true
        with:
          watch: true
          watch-minutes: 110
          saving-jobs: |
            ^gate( / build \(.*\))?$
```

- "Re-run failed jobs" re-runs a failed build job, which saves a new entry, but not the watch job,
  which succeeded in the first attempt. A workflow of its own on `workflow_run` (the CI workflow,
  `completed`, branch main) with one job that holds `actions: write` and runs
  `actions/cache-prune` once removes the replaced entry after every attempt.

- After the restore, `actions/restore-mtimes` gives every tracked file an old mtime and touches
  the files that differ from the commit the entry was built from (`.ci-cache-meta/commit`, saved
  with the entry), so Cargo's fingerprints see exactly those as changed. The craft-fonts and
  craft-i18n checkouts keep old mtimes while they are at the commits recorded with the entry.
  Without a marker, or when that commit cannot be fetched, every source stays new and Cargo
  rebuilds all workspace crates.
- `cold-build: true` restores nothing and builds everything, and where the cache saves it writes a
  fresh entry. Run it on a schedule as the safety net for the restored mtimes; it also starts the
  rolling entry afresh, which drops artifacts of older toolchains and lockfiles.
- The lint legs keep their usual entry. An app job restores the rolling entry with
  `actions/rust-cache`'s `cache-workspace-crates: "true"`.
- `lint-cache: false` runs the lint legs without a dependency cache (they restore and save
  nothing), which frees about 1 GB per OS of the repository's cache budget for slower lint legs.

With `select-os`, a pull request runs those legs (macOS and Windows, say) only when it can change
platform behaviour; pushes, schedules and manual runs still run every leg. A `select legs` job
(`actions/select-platforms`) compares the merge commit with its first parent and runs every leg
when a changed file is

- in a workspace crate whose `.rs` files or `Cargo.toml` hold a `cfg` naming `windows`, `unix`,
  `target_os`, `target_vendor` or a `windows`/`unix` `target_family` (or a crate in
  `select-crates`), or in any crate that depends on one (`cargo metadata --no-deps`);
- part of the build setup (`.github/`, `Cargo.lock`, the root `Cargo.toml`, `rust-toolchain`,
  `.cargo/`, `.config/`) or matches `select-paths`;
- outside every workspace crate and not documentation (`*.md`, `docs/`, `LICENSE*`).

A pull request labelled `select-label` (`full-ci` by default) runs every leg. The label is read
when the run starts: after adding it, push or re-run from a new event (a manual run of the
branch) to get the other legs. Legs that are not selected don't appear in the checks; a platform
problem they would have caught shows up in the next push to main, which runs every leg.

```yaml
    with:
      os: '["ubuntu-latest","windows-latest","macos-latest"]'
      select-os: '["windows-latest","macos-latest"]'
```

Set `artifact-name` and `artifact-path` to upload outputs after the command and lockfile checks pass.
Only the unsharded test/build leg uploads, never lint, cache-warm, or test-shard legs. Each OS uses
`<artifact-name>-<matrix.os>`; missing files fail, retention is one day, and empty defaults do nothing.
Callers must choose distinct names for different feature configurations or producer jobs.

After the gate, the job fails if `Cargo.lock` changed. Legs run with `fail-fast: false`; each has
`timeout-minutes` (default 60). A job only some apps need, such as a web build or a Windows-only
check, can be a second call with its own `command`, `os`, `tools` and `targets`, and its own
`cache-version` so its cache (another profile or target) doesn't share the main gate's key:

```yaml
  web:
    uses: storytold/craft-common-actions/.github/workflows/gate.yml@main
    with:
      cache-version: v1-web
      os: '["ubuntu-24.04"]'
      targets: wasm32-unknown-unknown
      craft-fonts-ref: ""
      tools: trunk@0.21.14
      command: packaging/web/package.sh
```

`setup-command` is also passed through the environment, then run with Bash from the checkout root.
Use it for caller-specific test inputs whose contents are pinned and verified by the caller's
fetcher. For example, a corpus-enabled workspace can fetch its locked corpus before running the
gate and export `CRAFT_PSD_CORPUS` through `$GITHUB_ENV`; a failed fetch stops the job before tests.

#### Caller Example:
```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

# A newer push to a PR branch cancels its older run. Every push to main gets its own run, so each
# state of main is checked to the end.
concurrency:
  group: ci-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  gate:
    # Tracks @main so suite-wide CI fixes reach every app without a PR in each repository.
    uses: storytold/craft-common-actions/.github/workflows/gate.yml@main
    with:
      packages: >-
        pkg-config libxkbcommon-dev libwayland-dev libx11-dev libxcb1-dev libxcursor-dev
        libxrandr-dev libxi-dev libfontconfig1-dev libfreetype-dev mesa-vulkan-drivers
```

Concurrency belongs to the caller: a reusable workflow cannot set it for the run.

The gate default and sharded SonarCloud coverage pin cargo-nextest to **0.9.150**, which fixes
[GHSA-vc25-qg58-w57c](https://github.com/nextest-rs/nextest/security/advisories/GHSA-vc25-qg58-w57c).
A caller overriding `tools` must also update its own nextest pin; changing the shared default
cannot override it. Set a required minimum at the top of the caller's `.config/nextest.toml`,
keeping its existing profiles and test groups:

```toml
nextest-version = "0.9.150"
```

A `recommended` minimum only warns; it does not enforce this requirement. The minimum-version
check does not upgrade a developer's installed executable, and can be explicitly overridden.
Do not enable experimental run recording or rerun untrusted portable recordings with an
affected version. Ordinary compiled test archives used by coverage are a different feature.

An app that also checks Windows on main, on manual runs and on pull requests labelled `full-ci`,
with per-OS environment and a shorter Windows gate (PDFCraft's call):

```yaml
jobs:
  gate:
    uses: storytold/craft-common-actions/.github/workflows/gate.yml@main
    with:
      os: ${{ (github.event_name != 'pull_request' || contains(github.event.pull_request.labels.*.name, 'full-ci')) && '["ubuntu-24.04","windows-latest"]' || '["ubuntu-24.04"]' }}
      craft-fonts-ref: 3d9ab3a132ed0eff457e424226a435a901da30d3
      packages: >-
        libxkbcommon-dev libwayland-dev libx11-dev libxrandr-dev libxi-dev libgl1-mesa-dev
        libgtk-3-dev qpdf poppler-utils
      tools: cargo-nextest@0.9.150,cargo-deny@0.20.2
      linux-env: PDFCRAFT_ORACLES_REQUIRED=1
      windows-env: PDFCRAFT_REQUIRE_WINRES=1
      # Windows checks what differs by platform: the build (with resources) and the tests.
      windows-command: |
        cargo clippy --workspace --all-targets --locked -- -D warnings
        cargo test --workspace --locked
```

### 7. `ast-grep.yml`

Structural lint with [ast-grep](https://ast-grep.github.io/), driven by the app's own
[`sgconfig.yml`](https://ast-grep.github.io/reference/sgconfig.html): CI and a local
`ast-grep scan` read the same rules. The rule tests in its `testConfigs` run first
(`ast-grep test --skip-snapshot-tests`), so a rule that stopped matching what it should fails
before it judges the code. The scan parses without building, so it takes seconds and can run on
every pull request next to the gate.

Findings show up as annotations on the pull request; a failing scan also lists every finding as
`file:line` in the log, since GitHub shows only the first few annotations of a step. An
error-severity finding fails the job. ast-grep is pinned and its download checked against a
SHA256 kept in the action, since its releases publish none; pass `sha256` with another `version`.

#### Caller Example:
```yaml
jobs:
  ast-grep:
    uses: storytold/craft-common-actions/.github/workflows/ast-grep.yml@main
```

With the config and rules elsewhere, scanning only some paths:

```yaml
jobs:
  ast-grep:
    uses: storytold/craft-common-actions/.github/workflows/ast-grep.yml@main
    with:
      config: lint/sgconfig.yml
      paths: apps crates
```

### 8. `changed-paths.yml`

Whether a run's changes touch any of `paths` (bash globs, one per line; `*` also matches `/`), or
its pull request carries `label`. Its `matched` output (`'true'`/`'false'`) lets an app run an
expensive job, such as a Windows gate leg, only when it can matter. Pull requests compare GitHub's
merge commit with its first parent, so only the pull request's own changes count; pushes compare
`before` with the pushed commit; any other event, a new branch, or a `before` that can't be fetched
matches. The checkout is two commits deep, a blobless partial clone that checks out only `.github`
and the root-level files.

To let adding the label start the job, list `labeled` in the caller's `pull_request` types and keep
that workflow's concurrency group separate from the main gate's, so a label never cancels it.

#### Caller Example:
```yaml
jobs:
  windows-scope:
    uses: storytold/craft-common-actions/.github/workflows/changed-paths.yml@main
    with:
      label: full-ci
      paths: |
        crates/wincolor/*
        crates/ui-egui/*
  gate-windows:
    needs: windows-scope
    if: needs.windows-scope.outputs.matched == 'true'
    uses: storytold/craft-common-actions/.github/workflows/gate.yml@main
    with:
      os: '["windows-latest"]'
      windows-command: cargo test -p my-windows-crate
```

### 9. `git-sources.yml`

Fails unless the app builds only from what its `Cargo.lock` names, and every git dependency in it
comes from `github.com/storytold`: `Cargo.lock` exists and is up to date with the manifests
(`cargo metadata --locked`), and neither `.cargo/config.toml` nor the legacy `.cargo/config`
replaces, patches or overrides a source (none of that shows up in `Cargo.lock`). It reports every
problem it finds. This replaces the "Git dependencies come from storytold" job apps carried in
their own `ci.yml`, `nix.yml` or `release.yml`; `working-directory` points it at a workspace that
isn't the repository root.

#### Caller Example:
```yaml
jobs:
  git-sources:
    uses: storytold/craft-common-actions/.github/workflows/git-sources.yml@main
  release:
    needs: git-sources
    uses: storytold/craft-common-actions/.github/workflows/release.yml@main
```

### 10. `house-rules.yml`

Fails unless `AGENTS.md` carries the shared agent rules from
[storytold/craft-contrib](https://github.com/storytold/craft-contrib) unchanged: one `craft-agents`
block holding `standards/app-agents.md` (RFC-0072). The block names the commit it was copied from and
the SHA-256 of the bytes inside it. The check loads `standards/app-agents.md` at that commit, requires
the commit to be on `main`, and compares the normalized bytes. An edited block, or a commit that is not
on `main`, fails. An intact older commit warns only when the normalized file changed on `main`. A
source that cannot be read fails as "could not check". The RFC-0006 `craft-house-rules` blocks are
retired: a file that still carries them fails and is moved with
`python3 actions/house-rules/house_rules.py write-agents --directory <app> --source <craft-contrib>`.
`rust` only applied to the retired blocks; it is still accepted, so callers need no change.

The caller is only the workflow call. The reusable workflow checks the app out and checks
craft-contrib out at `main` with full history.

#### Caller Example:
```yaml
jobs:
  house-rules:
    uses: storytold/craft-common-actions/.github/workflows/house-rules.yml@main
    with:
      rust: true
```

### 11. `docker-build.yml`

Builds one image per entry in `platforms`, pushes every platform by digest to each repository in
`images`, then joins those digests into a single multi-arch manifest per repository. It logs in only
where an image needs it: `ghcr.io` through the workflow token, `docker.io` when Docker Hub images
are listed (using `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`), and `dhi.io` when `dhi-login: true`
and the Docker Hub secrets are provided.

Both craft web images publish this way. `font-preview-service` fetches its font sources into the
build context first, so it passes a `pre-build-command`; `artcraft-services` builds one platform on a
larger runner, passes `build-args` and a GHA layer cache, and sets `free-disk: true` because the Rust
release build needs the space.

#### Caller Example:
```yaml
name: Docker publish

on:
  push:
    branches: [main]
    tags: ["v*"]
  workflow_dispatch:

permissions:
  contents: read
  packages: write

concurrency:
  group: docker-publish-${{ github.ref }}
  cancel-in-progress: true

jobs:
  publish:
    uses: storytold/craft-common-actions/.github/workflows/docker-build.yml@main
    with:
      images: |
        ghcr.io/storytold/artcraft-services
        docker.io/storytold/artcraft-services
      platforms: '[{"platform":"linux/amd64","runner":"github-ubuntu-64core"}]'
      dockerfile: build/service_cpu.Dockerfile
      build-args: |
        GIT_SHA=${{ github.sha }}
      free-disk: true
      cache-from: type=gha
      cache-to: type=gha,mode=max
    secrets: inherit
```

The caller owns triggers, concurrency and `packages: write`; a called workflow cannot raise its
owner's permissions. Tags default to the branch name, the semver tag and `latest` on the default
branch, and the `tags` input replaces those metadata-action rules. `timeout-minutes` (default 60)
bounds each platform build; the manifest merge has its own 15-minute limit.

### 12. `agent-roots-conformance.yml`

Verifies Rule 18 startup confinement on a built `<app>-cli` artifact uploaded by `gate.yml` (or another build job). It runs the binary against temporary directories and fails unless the entry point refuses to start with no roots, only `--automation-read-root`, only `--automation-write-root`, a missing read root, or a missing write root, while accepting both valid roots and answering an MCP JSON-RPC `initialize` request. When `connect-flag` is set (for example `--connect`, `--bridge`, or `--attach`), it also binds a local loopback control-channel stub that advertises no roots and requires the CLI to exit non-zero before answering MCP `initialize`.

#### Caller Example:
```yaml
jobs:
  agent-roots:
    needs: gate
    uses: storytold/craft-common-actions/.github/workflows/agent-roots-conformance.yml@main
    with:
      artifact-name: wordcraft-cli-ubuntu-24.04
      binary-path: wordcraft-cli
      connect-flag: --connect
```

## Composite Actions

Individual actions are also available for composing custom pipelines:

- `actions/checkout-craft-fonts`: Checks out `storytold/craft-fonts` at a designated ref.
- `actions/checkout-craft-i18n`: Checks out `storytold/craft-i18n` at the caller's `craft-i18n.rev`, if any, and sets `CRAFT_I18N_DIR` and `CRAFT_I18N_REQUIRED=1`.
- `actions/resolve-version`: Slices version from `packaging/env.sh` and validates format.
- `actions/install-wix`: Installs WiX toolset and UI/Util extensions.
- `actions/install-nfpm`: Installs `nfpm` with strict SHA256 checksum verification.
- `actions/install-linux-deps`: Installs Linux system build libraries and nfpm.
- `actions/install-apt-packages`: Installs the given Ubuntu packages and keeps the downloaded `.deb` files in the Actions cache, so later runs install without the mirror download. By default only pushes to the default branch save the cache; PRs restore it. For an app's CI:

  ```yaml
  - uses: storytold/craft-common-actions/actions/install-apt-packages@main
    with:
      packages: pkg-config libxkbcommon-dev libwayland-dev mesa-vulkan-drivers
  ```
- `actions/configure-apt-mirrors`: Points the runner's Ubuntu archive sources at a mirror list that reaches beyond ubuntu.com, with the mirrors that answer first: the image's own mirrors (azure.archive, archive and security.ubuntu.com, or ports.ubuntu.com on arm), then `mirrors.edge.kernel.org` and `mirrors.dotsrc.org` (amd64) or `mirrors.dotsrc.org` and `ftp.tu-chemnitz.de` (arm). `install-apt-packages` runs it before every download attempt; run it before a plain `apt-get update`. A job whose Harden-Runner policy blocks egress must allow those hosts on port 443.
- `actions/install-riscv64-cross`: Installs Ubuntu ports RISC-V multiarch and cross-compilers. amd64 and riscv64 come from one pair of mirrors serving the same snapshot: Ubuntu's own (archive, security and ports.ubuntu.com), then `mirrors.dotsrc.org` for both, then `mirrors.edge.kernel.org` with `ftp.tu-chemnitz.de` (ports); the image's own Ubuntu sources are disabled in that job. A job whose Harden-Runner policy blocks egress must allow those hosts on port 443.
- `actions/install-trunk`: Installs Trunk for WebAssembly builds.
- `actions/draft-release`: Collects artifacts, signs the Linux packages when given `signing-key`, calculates (and signs) SHA256 checksums, and publishes/updates draft GitHub release.
- `actions/import-signing-key`: Imports an ASCII-armored OpenPGP signing key into a private `GNUPGHOME` and exports `GNUPGHOME`, `SIGNING_FPR` and `SIGNING_PUBKEY`; an empty key does nothing.
- `actions/import-macos-certificate`: Imports a Developer ID Application certificate into a private temporary keychain and exports `MACOS_SIGN_IDENTITY` and `MACOS_KEYCHAIN` (see "macOS code signing").
- `actions/packaging-lint`: Steps for packaging linter.
- `actions/windows-install-policy`: Builds and checks metadata-only Windows installer fixtures, or checks an existing MSI against the caller's installation policy (see "Windows installation policy").
- `actions/setup-windows-signing`: Exports the shared Authenticode signing script (`CRAFT_SIGN_SCRIPT`) for the app's packaging step (see "Windows code signing").
- `actions/ast-grep`: Installs a pinned ast-grep, runs the caller's rule tests and scans with its `sgconfig.yml` (see `ast-grep.yml`).
- `actions/export-app-build-env`: Sets the app's own `<PREFIX>_VERSION`, `<PREFIX>_BUILD_DATE` and `<PREFIX>_BUILD_SHA` for the steps after it, with the prefix its `packaging/env.sh` reads (or `env-prefix`); its `names` output lists them for a VM's `envs`.
- `actions/check-git-sources`: The check `git-sources.yml` runs, for a job that has already checked out the app; `allowed-prefix` changes where git dependencies may come from (default `git+https://github.com/storytold/`).
- `actions/rust-cache`: The gate's rust-cache dependency cache, with the key formula in one place. It hashes the canonical root `[profile]` table, then rust-cache keys on the lockfile, member manifests, toolchain, environment, OS and architecture (see "Gate cache reuse"). `save-if` defaults to `false`, so it restores without writing, and only an accepted `true` writes: an empty, whitespace-only or unrecognized value (for example the text of an expression rather than its value) restores only; `job` selects the entry to use, which is the calling job unless an app's own job names the gate's (`job: gate`); `align-environment` keeps the `CARGO_HOME` a toolchain action exports out of the key, and refuses a cargo home other than `~/.cargo`. A self-hosted runner whose `CARGO_HOME` is a crate cache of its own (the cluster runners' node cache) names that directory in `CRAFT_RUNNER_CARGO_HOME`; when the job's `CARGO_HOME` is that directory, with or without `align-environment`, rust-cache gets a job-private directory instead, so it never restores into, prunes or saves the runner's cache, and the entry holds only the target directory. Those entries are keyed apart from GitHub-hosted legs' entries. A `CARGO_HOME` the job sets itself is still refused with `align-environment`. A `CARGO_TARGET_DIR` outside the checkout (such as a short `D:\t` on Windows) is passed to rust-cache as a relative path, so it still restores and saves. A failed job saves nothing unless `cache-on-failure: true`: a job that fails before its test build would otherwise leave a half-filled entry that rust-cache never replaces. `cache-workspace-crates: "true"` uses a rolling entry that also keeps the workspace crates' compiled libraries and unit-test executables: it restores the newest entry under the job's prefix and, where `save-if` allows, saves an entry of the run's own, named after the entry it restored (`<job>-crates-<os>-<arch>-<parent run id>-0`, so a second child of one entry is refused), since rust-cache never replaces an existing key; a saving leg also gets `CRAFT_CACHE_UNITS` for `actions/cache-trim`; `restore: "false"` skips the restore. Pair it with `actions/restore-mtimes`. Use it instead of calling `rust-cache` directly, so a job cannot drift from the gate's key:

  ```yaml
  - uses: storytold/craft-common-actions/actions/rust-cache@main
    with:
      cache-version: v2 # Match the gate call; bump both together.
      job: gate
      align-environment: true
  ```
- `actions/cache-trim`: For a leg that saves the dependency cache (gate.yml runs it). Trims the target directory's build units (`<profile>/build/<package>/<hash>/`, Cargo's unit layout) using the build's record in `CRAFT_CACHE_UNITS`, before rust-cache's post step saves. `actions/rust-cache` sets `CRAFT_CACHE_UNITS` in a leg that saves; the build appends Cargo's JSON messages to it: run cargo with `--message-format json-render-diagnostics` (nextest: `--cargo-message-format json-render-diagnostics`), which keeps the diagnostics readable on stderr, and append the stdout lines that start with `{"reason":`. Record every cargo command of the leg, the one that builds a cargo-alias tool included. When an unrecorded unit was built in this job (a file newer than the start time actions/rust-cache writes beside the record), the trim keeps it and warns with the unit name. It still removes unrecorded units from earlier jobs, so an incomplete record does not save all restored units. An unrecorded command that used one of those restored units will rebuild it on the next run. Without a record it changes nothing. A trim that fails warns and exits 1; gate.yml runs it with `continue-on-error`, since the failure leaves a usable cache.
- `actions/rust-channel`: Names the toolchain, targets and components a job installs: those of the checkout's `rust-toolchain.toml` (`channel`, `targets`, `components`) when it has one, merged with the caller's `targets` and `components`, else the caller's `toolchain` (default `stable`). Pass its outputs to the toolchain action (`toolchain`, `targets`, `components`) right after the checkout. rustup runs cargo with the file's channel, so a job that installs `stable` beside a file that names another release would make rustup fetch that release without the job's targets. Apps without the file get what they asked for. release.yml and windows-arm64.yml use it.
- `actions/cache-prune`: For a caller job with `permissions: actions: write`. Deletes a branch's cache entries that no run will restore again: all but the newest rolling `cache-workspace-crates` entry per job, OS and architecture; and of the other rust-cache entries, and of `<name>-<sha256>` entries for the names in `content-keys`, each older entry once nothing has used it since the newest of its family was created. Other entries (apt packages, tool caches) stay. Families leave out the environment and lockfile hashes, so a toolchain or lockfile update replaces the old entry; two jobs sharing one key name with different toolchains or environments would count as one family. `watch: true` runs beside the run's jobs and prunes each time one of `saving-jobs` (regular expressions on the jobs API's names, matching the skipped job's name too) finishes, ending when none is queued or running, once the cache list shows an entry created after the last one started (at most three more 30-second polls), or after `watch-minutes`, with one more pass; `dry-run: true` only lists. A failed pass warns and the next one tries again; a failure that stops the script warns and exits 1, so run it with `continue-on-error: true`.
- `actions/restore-mtimes`: For a `cache-workspace-crates` entry. `mode: record`, in the job that saves the entry, writes the commit it was built from and the commits of the craft-i18n and craft-fonts checkouts to `.ci-cache-meta`, which the entry keeps. `mode: restore`, after the restore, gives every tracked file an old mtime and touches the files that differ between that commit and HEAD, so Cargo rebuilds only the units that read a changed file; the external checkouts get old mtimes only while they are at the recorded commits. Without a marker, or when the commit cannot be fetched, it changes nothing and Cargo rebuilds every workspace crate.
- `actions/gate-setup`: The setup a `gate.yml` job runs after its checkout: craft-fonts and craft-i18n, the caller's environment, Linux packages, the Rust toolchain and cargo tools, the dependency cache through `actions/rust-cache` (with `actions/restore-mtimes` for `cache-workspace-crates`), sccache, and the caller's setup command and its cache. It takes `gate.yml`'s inputs plus which parts a job needs; other workflows have no use for it.
- `actions/gate-run`: Runs one of `gate.yml`'s commands with bash; with `timeout-minutes`, GNU `timeout` stops the command after that many minutes, and the log lists the processes still running a minute before.
- `actions/select-platforms`: The legs a pull request needs, for `gate.yml`'s `select-os` (see `gate.yml`); an app job that runs its own platform legs can call it after a `fetch-depth: 2` checkout and read its `os`, `platform` and `reason` outputs. With `select-tests: true` it also selects the tests: `tests` (`all`, `some` or `none`), `filter` (a nextest filterset) and `skip` (the members whose tests do not run). A pull request runs the tests of the crates that own a changed file, of the crates that declare a read of it, and of every crate that depends on those by any dependency kind; the build setup, `paths`, a crate's own `Cargo.toml` or a non-documentation file outside the crates (`docs` adds documentation globs) runs every test, and documentation alone runs none. Pass the filter to `cargo nextest run --workspace ... -E "$filter"`, so the build stays the whole workspace and only the run is narrowed. A crate whose build or tests read another crate's files declares them in its `Cargo.toml`:

  ```toml
  [package.metadata.craft]
  reads = ["crates/cms/profiles"]
  ```

  The same step fails when a crate's sources name a path into another crate's directory (an `include_str!`/`include_bytes!` path, or a `../` literal in a file that uses `CARGO_MANIFEST_DIR`) that `reads` does not cover, or when a declared path matches no tracked file.
- `actions/house-rules`: The check `house-rules.yml` runs. `source` is a checkout of craft-contrib with `main` history (refresh `origin/main` before checking; the local `main` ref is used only when `origin/main` is absent, never `HEAD`); `require-rust` applied only to the retired house-rules blocks and is ignored.
- `actions/agent-roots-conformance`: The Rule 18 startup confinement check `agent-roots-conformance.yml` runs. Takes a local `binary` path, optional `subcommand` (default `mcp`), `extra-args`, `connect-flag`, `connect-address-format` (`host:port` or `port`), `connect-extra-args`, and `timeout-seconds`. Can also be called inside an app's existing Linux build job before uploading artifacts.

## Storytold Organization Hosting

This repository is hosted at `storytold/craft-common-actions` for all Storytold / ArtCraft craftapps.

## App-specific packaging checks

Keep app-specific packaging assertions in the caller repository and pass their
Bash script to `packaging-lint.yml` with
`extra-checks-script: packaging/check-app-specific.sh`. It runs after the shared
lint action, with the checkout and lint tools available. A missing or failing
requested script fails the job. Linux static lint does not replace native Windows
installer probes; those belong in a shared workflow here as well.

An app's `.github/workflows/` only calls this repository's workflows at `@main`,
with inputs and secrets: no caller jobs with their own `runs-on`/`steps`. App-specific
checks (fuzzing, corpora, web builds, installer probes) become shared workflows or
inputs here. Jobs that apps still run themselves move here without being dropped:
an app removes one only once a shared workflow runs it.

## Windows installation policy

An app with `packaging/windows/install-policy.json` checks its installer with:

```yaml
jobs:
  windows-install-policy:
    uses: storytold/craft-common-actions/.github/workflows/windows-install-policy.yml@main
```

It installs WiX 5.0.2 and the extensions the policy lists, builds metadata-only MSIs (plain-text
payloads) for x86, x64 and ARM64 from the app's own WiX sources, and checks their scope, folders,
upgrade identity, registry entries and wizard. Nothing is installed and the app isn't built. Paths in
the policy are relative to the packaging directory (`packaging-directory`, default
`packaging/windows`), and the icon to the app root two levels above it. An app's
`test-legacy-fixture.ps1` there runs first, if it has one.

This checks an app's own installer against its own policy. It is for MSIs from the caller's own
build, or for this repository's fixtures, and is not a general inspector for third-party MSI
packages. To check the MSI an app's packaging job just built, that job uses the action with
`msi-path`:

```yaml
- uses: storytold/craft-common-actions/actions/windows-install-policy@main
  with:
    msi-path: dist/MyApp-x64.msi
```

The reusable workflow runs the action from its own repository and commit (`uses: $/...`), so the
two always match: a caller that calls the workflow `@main` gets the action at the same `main`
commit, and one that pins the workflow to a commit gets the action from that commit. A job that
uses the action directly picks its own revision; use the same one as the workflows it calls. The
fixture test in this repository runs the local action on both fixtures, and also calls the
reusable workflow on one of them, then reads that job's log to check that the action was the copy
downloaded at the workflow's commit and the installer came from the caller's checkout. There the
workflow and the caller are the same commit; the first app to move to the workflow is what
confirms the pairing across repositories.

The build defines `Version`, `DisplayVersion`, `BinDir` (the policy's `payloads`) and `IconPath`.
Anything else the app's WiX reads goes in the policy's `wixVariables`: a literal value, or a
placeholder the build creates, `@folder [file ...]`, `@bitmap WIDTHxHEIGHT` or `@rtf`:

```json
"wixVariables": { "DocsDir": "@folder", "BannerBmp": "@bitmap 493x58", "HasCli": "1" }
```

Optional policy keys; nothing here knows about particular apps:

| Key | Meaning | Default |
| --- | --- | --- |
| `suiteFolder` | Folder that groups the suite's apps under Program Files and the per-user programs folder | `ArtCraft` |
| `skipsLicencePage` | The wizard goes straight from Welcome to the install folder; the checker verifies that routing in both directions | `false` |
| `mode` | Only `dualScope` (the wizard offers per-user or per-machine) is checked; a per-user-only installer keeps its own check for now | `dualScope` |

When an app moves to the shared workflow it replaces only its `windows-install-policy` job, and keeps
the pre-signing check in its `package.ps1` until its packaging calls the shared checker.

The test workflow builds two made-up apps in `tests/fixtures/windows-install-policy` (one with its
own wizard, one with the WiX UI wizard) and checks that mismatched or incomplete policies fail.

## macOS code signing

Every app used to carry its own copy of `packaging/macos/import-cert.sh`, and the copies drifted:
some still wrote the signing certificate to a predictable temporary path. `import-macos-certificate`
is the maintained version, so a fix lands once.

Replace the app's import step with the action. It sets the same
`MACOS_SIGN_IDENTITY` and `MACOS_KEYCHAIN` variables that `package.sh` already reads:

```yaml
- uses: storytold/craft-common-actions/actions/import-macos-certificate@main
  with:
    certificate: ${{ secrets.APPLE_CERTIFICATE }}
    certificate-password: ${{ secrets.APPLE_CERTIFICATE_PASSWORD }}
    keychain-password: ${{ secrets.KEYCHAIN_PASSWORD }}
```

The certificate and keychain go in a fresh private directory with an unpredictable name. The
decoded bundle is deleted as soon as it is imported, and a failed import also deletes the keychain.
Without a certificate it only warns and exits successfully, so unsigned test builds still work.

The shared `release.yml` imports the certificate with this action, so an app's own
`packaging/macos/import-cert.sh` is no longer run and can be deleted. It calls the action as
`$/actions/import-macos-certificate`, from the workflow's own repository and commit: an app that
calls `release.yml@main` gets main's action, and one that pins `release.yml` to a commit signs with
that commit's action.

## Job time limits

Limits live on executable jobs in these reusable workflows, since caller jobs
using `uses` cannot set `timeout-minutes`.

| Work | Limit (minutes) |
| --- | ---: |
| Packaging lint | 15 |
| Windows installation policy | 10 |
| Release version / draft publication | 10 |
| Release macOS, Windows, Linux, Flatpak, web, RISC-V and server builds | 90 |
| Release FreeBSD | 300 |
| Standalone FreeBSD build, test and optional package | 300 |
| Windows ARM64 package / install | 90 / 30 |
| CI gate, per leg (`timeout-minutes` input) | 60 |
| Changed paths | 5 |
| Git sources | 5 |
| Agent roots conformance | 10 |
| Docker build, per platform / manifest merge | 60 / 15 |
| SonarCloud, including coverage (`timeout-minutes` input) | 90 |

A gate that hangs (a test deadlocked, a process waiting on a window) would run until the job
timeout, and GitHub only reports the job as cancelled. Pass `gate-timeout-minutes`, a few minutes
below `timeout-minutes` (for example 70 with `timeout-minutes: 85`), and the gate command is
stopped at that time instead (GNU `timeout`, then killed 30 s later). The leg fails with a
`Gate timed out` error, and a log group lists the processes still running one minute before the
deadline. The default, 0, sets no limit of its own. Windows legs use Git Bash's `timeout`.

SonarCloud callers whose instrumented workspace tests exceed the 90-minute
default can pass `timeout-minutes: 120` under the calling job's `with`. This
changes the job's time budget without dropping tests or coverage. Pass
`fonts-ref` at the app's tested craft-fonts commit when its tests need those
fonts. The pinned fonts are available both when compiling the test archive and
when running tests; a requested font pin also sets `CRAFT_FONTS_REQUIRED=1`
so missing embedded fonts fail explicitly. Callers with `scripts/fetch-pdf-test-fonts.py` can also pass
`pdf-test-fonts: true` to provision their pinned PDF fixtures outside the checkout
and set `CRAFT_PDF_TEST_FONT_DIR` before archive compilation and either test runner. Fixture provisioning
failures stop that job and prevent the dependent scan. The scan still needs to finish and pass its quality gate.

Sharded Rust coverage continues after test failures when the shard produced coverage profiles, so
SonarCloud can still analyse the code. Each shard reports its test outcome in the job summary;
failing tests also produce a warning annotation pointing to the test log. A green coverage job
therefore does not mean all tests passed. If a shard produces no profiles, its summary records
that the scan cannot include it and the shard fails. Runner errors also fail the shard.

Callers with Python tooling can pass both `python-coverage-source` (a source directory)
and `python-coverage-script` (the test script). The scan job runs that script under
pinned coverage.py in an isolated virtual environment and writes `python-coverage.xml`.
Both paths must be inside the checkout and contain no commas or glob characters.
The environment contains only coverage.py and the runner’s Python standard library;
caller coverage configuration is ignored so parallel-data settings cannot hide the report.
Set `sonar.python.coverage.reportPaths=python-coverage.xml` in the caller's Sonar
configuration and classify its test files as tests. The script runs in full, but
is omitted from the production coverage report. Missing inputs, failed tests or
report generation stop the job; empty inputs leave existing callers unchanged.
This step requires the Sonar token, so token-independent CI should also run the tests.

Workspaces run their tests as four parallel `cargo nextest` partitions by
default. One job builds the instrumented test archive; each partition runs from
that archive, merges its own raw profiles into one sparse `.profdata` with the
rustup `llvm-profdata`, and uploads that plus a small test-exit-status file; the scan job merges the shard
profiles and exports them with `llvm-cov`. A raw profile that `llvm-profdata`
cannot read (for example one cut short by a test process that crashed while
writing it) fails its shard and is named in an error annotation, rather than
reaching the scan. Pass `shards: 1` to keep the single `cargo llvm-cov` job.
Sharded runs skip doctests, because nextest does not run them.
The instrumented archive build and the unsharded `cargo llvm-cov` path default to two
parallel rustc jobs (`build-jobs`, 1–4). A cold llvm-cov workspace build can otherwise
start enough jobs to exhaust a hosted runner and shut it down mid-compile (exit 143).
Each shard links the build job's target directory to its extracted copy of the
archive, so tests that run a workspace binary through
`env!("CARGO_BIN_EXE_<name>")` find it as they do in the gate.
Ordinary test failures still allow coverage and static analysis to upload, but then
fail the scan job. Missing shard status also fails closed; a green analysis job
must not conceal failed tests. The single-shard Rust path is unchanged.

The scan job frees unused runner SDKs before either Rust coverage path. The archive build
frees them in the background, so the build starts at once. Sharded
coverage merges and removes the shard profiles before extracting the test archive, then
releases the compressed archive and temporary binaries as they are consumed.
Disk-space snapshots in the logs show headroom before cleanup and aggregation.

The new limits leave headroom over sampled Craft app job timings; the existing
FreeBSD and ARM64 limits are retained. Treat these as initial limits, review
successful cold builds after rollout and lower them when the evidence supports it.
These are per-job limits, not a total workflow duration or spend budget.

## Windows code signing

Every app used to carry its own copy of `packaging/windows/sign.ps1`, and the copies drifted: some
lacked the SHA-256 pin on the Azure Trusted Signing client. `setup-windows-signing` ships the
maintained version, so a fix lands once.

Signing happens inside the app's `package.ps1` (it signs the executable before the MSI
is built, then the MSI), so the action does not sign anything itself. It makes the shared script
available and the packaging script calls it with the signing secrets in its own environment, so the
secrets never pass through the action:

```yaml
- uses: storytold/craft-common-actions/actions/setup-windows-signing@main
  with:
    product-name: PhotoCraft   # signtool /d; /du defaults to the repository's GitHub URL
- run: ./packaging/windows/package.ps1 -Arch x64
  shell: pwsh
  env:
    WINDOWS_CERTIFICATE: ${{ secrets.WINDOWS_CERTIFICATE }}
    # ... the other WINDOWS_* and AZURE_* secrets
```

```powershell
# in package.ps1, where it used to call sign.ps1
& $env:CRAFT_SIGN_SCRIPT dist/app.exe dist/App.msi
```

The script prefers a PFX (`WINDOWS_CERTIFICATE`) and falls back to Azure Trusted Signing. Before
loading the Trusted Signing client it checks the downloaded package against a pinned SHA-256;
upgrading the client means changing the version and hash together. With neither kind of secret it
warns and leaves the files unsigned.

The shared `release.yml` and `windows-arm64.yml` run this action before `package.ps1`, with the
app's `app-title` as the product name (`windows-arm64.yml` falls back to `app-dir`), so
`CRAFT_SIGN_SCRIPT` is always set there. Both call it as `$/actions/setup-windows-signing`, so the
script comes from the same commit as the workflow. An app switches by making `package.ps1` call it
instead of its own `sign.ps1`, then deletes its copy; until then nothing changes for it.

## Testing workflow failure paths

With Python 3 and PyYAML installed, run:

```sh
python3 -m unittest discover -s tests -v
```

GnuPG signing fixtures need local Unix sockets for their temporary agents. They
report an explicit skip when a local sandbox denies socket creation or binding;
the same restriction remains a failure on GitHub Actions. Other GPG errors are
not skipped, and the hosted suite exercises the real protected-key fixture.

The tests execute the FreeBSD run template under the host's POSIX shell with
fixture build/test commands and real tar archives. They cover failures in each
custom test group, default build/test failures, missing/corrupt packages and font
environment propagation. They do not replace native FreeBSD caller runs.

### Gate cache reuse

The gate cache key includes the canonical root `[profile]` table, including per-package
profile overrides. Dependency and version edits no longer change the restore prefix;
`rust-cache` still keys on the lockfile, toolchain, environment and member manifests,
and Cargo validates restored fingerprints. The first run after adopting this key
is cold.

Only the default branch saves by default. A caller may set
`cache-save-on-dispatch: true` for repeated manual integration testing. This permits
`workflow_dispatch` runs to save a cache scoped by GitHub to that branch, never a
pull-request run. Use this selectively to avoid storage churn; sibling branches cannot
restore each other's caches. Do not pass the input until this workflow change has landed.

`gate.yml` gets its key formula from `actions/rust-cache`, and an app job that builds the same
workspace restores the gate leg's entry with the same action rather than copying the formula. Name
the producing job (`job: gate`); the action forms the entire profile-and-job fragment, and rust-cache
adds no further job id to that shared key. Pass the same `cache-version` literal as the gate call
(`v2` in the example), bump both together, leave `save-if` off, and set `align-environment: true` when
the job installs Rust with an action that
exports `CARGO_HOME`. rust-cache hashes every `CARGO*`, `CC*`, `CFLAGS*`, `CXX*`, `CMAKE*` and
`RUST*` variable it can see, and the exported `CARGO_HOME` is not part of the build, so an unaligned
job computes a different key and restores nothing. The job must otherwise build with the same
`CARGO_*`/`RUST*` values as the gate leg. Changing a toolchain or setup-action pin is a cache-contract
change: compare both legs' actual hashed environments and keys again. Do not call the action from an app until this lands on
`@main`; an app's pull request can only follow this one.

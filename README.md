# artcraft-common-actions

<a href="https://sonarcloud.io/project/overview?id=storytold_craft-common-actions"><img alt="Scanned by SonarCloud" src="https://img.shields.io/badge/Scanned_by-SonarCloud-4b9fd5?style=flat-square&logo=sonarqubecloud&logoColor=white"></a>

Reusable GitHub Actions workflows and composite actions for Storyteller / ArtCraft desktop and web applications.

This repository unifies and eliminates duplicate CI/CD and release workflow definitions across the ArtCraft ecosystem into DRY, reusable GitHub Actions.

## Workflows Included

### 1. `release.yml`
Full multi-platform release pipeline for Craft apps:
- **macOS universal** (x86_64 + aarch64 universal bundle, DMG, CLI, Developer ID codesign & notarization)
- **Windows** (x64, x86, ARM64 MSI installer with WiX v5, CLI, Azure Trusted Signing)
- **Linux** (x86_64, aarch64 AppImage, deb, rpm, tar.gz via nfpm)
- **Flatpak** (x86_64, aarch64 bundle generation)
- **FreeBSD** (FreeBSD 15.1-STABLE VM build & packaging for x86_64; aarch64 is cross-compiled in the same x86_64 VM and opt-in through `freebsd-architectures`, see #39)
- **Arch Linux** (x86_64, aarch64 `.pkg.tar.zst` built via `makepkg` from release tarball, named by bundle id; set `has-arch: false` to skip)
- **WebAssembly** (wasm32-unknown-unknown with Trunk)
- **Linux RISC-V 64** (cross-compilation for riscv64gc-unknown-linux-gnu, producing deb, rpm, and tar.gz via SHA256-verified nfpm; built on Ubuntu 24.04, so it sets `GLIBC_FLOOR=2.39` for the app's `nfpm.yaml` to declare, see #44)
- **Draft Release** (computes `SHA256SUMS.txt` and publishes/updates draft GitHub Release)

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
```

List every signing secret explicitly. The signing jobs run in the caller repo's `release` environment and read the values from there, but only for the names listed here: an unlisted name is empty. On the `release` branch the macOS and Windows jobs fail when their signing secrets are empty, rather than shipping unsigned builds.

macOS and Windows packages are signed by the app's own packaging scripts. `release.yml` does not request `id-token` or `attestations`, so existing callers keep building as they do now.

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
and builds with `CRAFT_I18N_DIR` and `CRAFT_I18N_REQUIRED=1`. Packaging `needs:` the
[i18n gate](#6-i18n-gateyml). A manual run can pass `skip-i18n-gate: true`; other events ignore it.
An app without `craft-i18n.rev` builds as before.

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

The release workflow builds FreeBSD aarch64 the same way, but only when the caller
sets `freebsd-architectures: '["x86_64","aarch64"]'` (the default is `'["x86_64"]'`).
Opt in only once the app's `packaging/freebsd/package.sh` builds with
`CARGO_BUILD_TARGET`, reads binaries from
`$CARGO_TARGET_DIR/$CARGO_BUILD_TARGET/release` and names its archive
`*-freebsd-aarch64.tar.gz`: the guest's `uname -m` is always `amd64`. The release
job fails if an archive does not match its matrix architecture. Apps without a
`package.sh` use the built-in packager, which already handles the cross target.

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
without a fallback. The test and release workflows use separate profile
namespaces. The workflow-file pattern fingerprints the caller's configuration;
changes to the shared workflow that need a new namespace must bump the cache
action's version prefix.

Cache data is restored on the host and explicitly transferred to the guest's
`/var/tmp/craft-cargo` and `/var/tmp/craft-target`. Only registry/git data and
`debug`/`release` artifacts are saved, excluding incremental state, Cargo
credentials/config and Rust toolchains. Successful non-PR runs save caches on a best-effort basis; PRs
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

### 6. `i18n-gate.yml`

Runs the app's `i18n_gate_*` tests, `cargo test --workspace i18n_gate -- --nocapture` with
`CRAFT_I18N_GATE=1`, against the craft-i18n commit in `craft-i18n.rev`, and prints their output as
the missing-translations report. It always succeeds: a missing translation shows in English, and a
failing `i18n_gate_*` test only adds a warning. Without `craft-i18n.rev` it does nothing.

`release.yml` runs it before packaging. `freebsd.yml` runs it with `pin-changes-only: true`: only
on a pull request whose diff touches `craft-i18n.rev`, whether or not `skip-tests` is set. An app
with its own CI workflow calls it the same way:

```yaml
jobs:
  i18n-gate:
    uses: storytold/craft-common-actions/.github/workflows/i18n-gate.yml@main
    with:
      pin-changes-only: true
```

### 7. `gate.yml`

The quality gate for an app's pull requests and pushes to main. It runs `command` (by default
`cargo xtask ci`) on each runner in `os`, after the suite's shared setup:

- the app and craft-fonts (at the suite's pin, with `CRAFT_FONTS_REQUIRED=1`) checked out without
  persisted credentials, and craft-i18n when the app has `craft-i18n.rev`;
- `packages` installed through `actions/install-apt-packages` on Linux legs;
- the channel from `rust-toolchain.toml`, or `toolchain`, with clippy, rustfmt and `targets`;
- prebuilt cargo tools from `tools` (by default `cargo-nextest`);
- `CARGO_PROFILE_DEV_DEBUG=line-tables-only` (set `dev-debug: ""` to keep the repository's
  profile) and the caller's `env`, `linux-env` and `windows-env` lines, each `KEY=VALUE`;
- a rust-cache dependency cache that only the default branch saves, so pull requests restore
  main's entry and never evict it. The key includes the root `Cargo.toml`, since rust-cache hashes
  only the members' manifests. Bump `cache-version` to start afresh.

After the gate, the job fails if `Cargo.lock` changed. Legs run with `fail-fast: false`; each has
`timeout-minutes` (default 60). Jobs only some apps need, such as fuzzing or web builds, stay in
the app's own workflow next to the call.

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
      tools: cargo-nextest@0.9.149,cargo-deny@0.20.2
      linux-env: PDFCRAFT_ORACLES_REQUIRED=1
      windows-env: PDFCRAFT_REQUIRE_WINRES=1
      # Windows checks what differs by platform: the build (with resources) and the tests.
      windows-command: |
        cargo clippy --workspace --all-targets --locked -- -D warnings
        cargo test --workspace --locked
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
- `actions/install-riscv64-cross`: Installs Ubuntu ports RISC-V multiarch and cross-compilers.
- `actions/install-trunk`: Installs Trunk for WebAssembly builds.
- `actions/draft-release`: Collects artifacts, calculates SHA256 checksums, and publishes/updates draft GitHub release.
- `actions/packaging-lint`: Steps for packaging linter.

## Storytold Organization Hosting

This repository is hosted at `storytold/craft-common-actions` for all Storytold / ArtCraft craftapps.

## App-specific packaging checks

Keep app-specific packaging assertions in the caller repository and pass their
Bash script to `packaging-lint.yml` with
`extra-checks-script: packaging/check-app-specific.sh`. It runs after the shared
lint action, with the checkout and lint tools available. A missing or failing
requested script fails the job. Native Windows installer probes remain separate
caller jobs; Linux static lint does not replace them.

## Job time limits

Limits live on executable jobs in these reusable workflows, since caller jobs
using `uses` cannot set `timeout-minutes`.

| Work | Limit (minutes) |
| --- | ---: |
| Packaging lint | 15 |
| Release version / draft publication | 10 |
| Release macOS, Windows, Linux, Flatpak, web, RISC-V and server builds | 90 |
| Release FreeBSD | 300 |
| Standalone FreeBSD build, test and optional package | 300 |
| Windows ARM64 package / install | 90 / 30 |
| CI gate, per leg (`timeout-minutes` input) | 60 |

The new limits leave headroom over sampled Craft app job timings; the existing
FreeBSD and ARM64 limits are retained. Treat these as initial limits, review
successful cold builds after rollout and lower them when the evidence supports it.
These are per-job limits, not a total workflow duration or spend budget.

## Testing workflow failure paths

With Python 3 and PyYAML installed, run:

```sh
python3 -m unittest discover -s tests -v
```

The tests execute the FreeBSD run template under the host's POSIX shell with
fixture build/test commands and real tar archives. They cover failures in each
custom test group, default build/test failures, missing/corrupt packages and font
environment propagation. They do not replace native FreeBSD caller runs.

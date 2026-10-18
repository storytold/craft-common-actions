# artcraft-common-actions

Reusable GitHub Actions workflows and composite actions for Storyteller / ArtCraft desktop and web applications.

This repository unifies and eliminates duplicate CI/CD and release workflow definitions across the ArtCraft ecosystem into DRY, reusable GitHub Actions.

## Workflows Included

### 1. `release.yml`
Full multi-platform release pipeline for Craft apps:
- **macOS universal** (x86_64 + aarch64 universal bundle, DMG, CLI, Developer ID codesign & notarization)
- **Windows** (x64, x86, ARM64 MSI installer with WiX v5, CLI, Azure Trusted Signing)
- **Linux** (x86_64, aarch64 AppImage, deb, rpm, tar.gz via nfpm)
- **Flatpak** (x86_64, aarch64 bundle generation)
- **FreeBSD** (FreeBSD 15.1-STABLE VM build & packaging for x86_64 and aarch64)
- **Arch Linux** (x86_64, aarch64 `.pkg.tar.zst` built via `makepkg` from release tarball, named by bundle id; set `has-arch: false` to skip)
- **WebAssembly** (wasm32-unknown-unknown with Trunk)
- **Linux RISC-V 64** (cross-compilation for riscv64gc-unknown-linux-gnu, producing deb, rpm, and tar.gz via SHA256-verified nfpm)
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
      craft-fonts-ref: abb83316d96aa59c1cf64784289e378fe9fa5695
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

`craft-fonts-ref` feeds the desktop builds. The web build gets craft-fonts too only with
`web-craft-fonts: true`: each app's `build.rs` decides which fonts its web build embeds (craft-fonts
`docs/integration.md`), so opt in once that list fits the app's web size budget.

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
Builds and runs workspace tests across a matrix (x86_64, aarch64) inside a FreeBSD 15 VM on a Linux runner.

`craft-fonts-ref` makes the checkout available to builds, default tests and custom
tests through `CRAFT_FONTS_DIR`. Set `require-craft-fonts: true` to also export
`CRAFT_FONTS_REQUIRED=1` and fail before building if the checkout is missing.
The application's build script validates its required font faces.

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
      craft-fonts-ref: 8dcdacd5153e64560d109541a47d806f26f048c0
```

### 5. `nix.yml`

Evaluates the repository's Nix flake across supported systems without building, validating flake evaluation and `Cargo.lock` synchronization.

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

## Composite Actions

Individual actions are also available for composing custom pipelines:

- `actions/checkout-craft-fonts`: Checks out `storytold/craft-fonts` at a designated ref.
- `actions/resolve-version`: Slices version from `packaging/env.sh` and validates format.
- `actions/install-wix`: Installs WiX toolset and UI/Util extensions.
- `actions/install-nfpm`: Installs `nfpm` with strict SHA256 checksum verification.
- `actions/install-linux-deps`: Installs Linux system build libraries and nfpm.
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
| Release FreeBSD | 180 |
| Standalone FreeBSD build, test and optional package | 300 |
| Windows ARM64 package / install | 90 / 30 |

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

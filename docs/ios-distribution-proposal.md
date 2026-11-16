# Opt-in Apple unsigned pipeline POC and distribution proposal

Status: **unsigned orchestration POC implemented on this proposal branch;
not adopted or proven on a hosted native app**. Signing/distribution remain
**unimplemented proposals**, 2026-11-16.
Tracking: [storytold/craft-common-actions#259](https://github.com/storytold/craft-common-actions/issues/259).

The accompanying `apple-unsigned.yml` is callable only by explicit opt-in; it adds
no automatic native runs to existing callers, credentials, production signing or publishing.
It does not select a suite mobile architecture. PdfCraft's motivating example is an
**unofficial, fork-only iPhone/iPad proof of concept (POC)**, not an official release.

## Existing integration and ownership

Reviewed shared `main` at `59d1ecc4ea15ab59f8d60d38b147d5016c1f1385`.
`release.yml` currently builds/signs/notarizes macOS packages, not iOS apps.
`gate.yml` supplies existing app checks; no iOS workflow is present.
Preserve these contracts and all default runner costs.

| Repository | Responsibility |
| --- | --- |
| `storytold/craft-common-actions` | Input validation, hosted-job isolation, toolchain setup, generic job-owned simulator creation/cleanup, driver invocation, evidence collection and contract tests; future protected signing orchestration only after separate approval |
| Each app repository | Reviewed Bash driver, native host/project generation, Rust libraries/FFI, sequential tests using supplied simulator UUIDs, unsigned build/test/package commands, app identity/privacy metadata, and separately reviewed thin caller workflows |
| App repository's protected environments | Credentials, allowed refs, deployment reviewers and approval history |
| `storytold/craft-contrib` / `storytold/craft-libs` | Suite architecture decisions and any accepted shared mobile host, separately reviewed |
| Apple Developer / App Store Connect | Legal owner, signing identities/profiles, app records, testers, beta review, App Review and release |

Related work: [RFC-0039](https://github.com/storytold/craft-contrib/pull/60),
[host proposal](https://github.com/storytold/craft-libs/pull/632),
[shared-crate inventory](https://github.com/storytold/craft-libs/issues/115),
[PdfCraft mobile request](https://github.com/storytold/pdfcraft/issues/2085),
[separate PdfCraft approach](https://github.com/storytold/pdfcraft/pull/1119),
[Android release proposal](https://github.com/storytold/craft-common-actions/issues/183),
and [desktop signing reuse proposal](https://github.com/storytold/craft-common-actions/pull/149).
None makes the SwiftUI POC an accepted suite standard.

Related shared history was searched across open **and closed** issues/PRs before
implementation, including merged certificate import
[#140](https://github.com/storytold/craft-common-actions/pull/140), merged
release dry-run [#208](https://github.com/storytold/craft-common-actions/pull/208),
open signing reuse #149 and the build/signing trust-boundary issue
[#7](https://github.com/storytold/craft-common-actions/issues/7).
Existing desktop import/export variables, pinned checkout/toolchain/artifact
actions, app-owned-script invocation and Python/YAML fixture testing are reused
as patterns. The new workflow is separate because release dry-run deliberately
skips the paid macOS release job; turning that job on would change caller costs
and signing behavior. This POC does not resolve #7 or modify production signing.

The [fork-only PdfCraft guide](https://github.com/blackoutsecure/pdfcraft/blob/docs/ios-poc-publishing/apps/ios/PUBLISHING.md)
and [blackoutsecure/pdfcraft#1](https://github.com/blackoutsecure/pdfcraft/pull/1)
record the example's unsupported controls and release blockers. That documentation
PR targets the fork POC branch, not upstream `main`; its latest local workspace/text
enhancements are not included in the published POC baseline or this proposal.

## Implemented unsigned driver contract

The reusable [apple-unsigned.yml](../.github/workflows/apple-unsigned.yml) invokes
the app's reviewed Bash driver as an argument array, never a command string:

```sh
bash scripts/build-apple.sh <build|test|package> <all|macos|ios-simulator|ios-device> <Debug|Release>
```

`scripts/build-apple.sh` is the default; `driver` may select another reviewed,
repository-relative `.sh` file. No absolute/traversal paths or command text.
The workflow resolves symlinks and rejects a driver outside the checkout.
Working directory is always the **app repository root**. Stage outputs and sanitized
test evidence below **`target/apple`**, not shared `dist/release`. Keep derived data,
Cargo build trees, credentials and unrelated caches outside that artifact root.

| Driver command | Required app-owned behavior |
| --- | --- |
| `build all` | Build macOS, iOS Simulator and iOS device code, including the correct Rust library/FFI for each target |
| `test all` | Debug-only relevant macOS tests, then iPhone **and** iPad simulator suites, sequentially; no physical-device claim |
| `test ios-simulator` | Debug-only iPhone and iPad suites, sequentially in this one job, using distinct required `CRAFT_IPHONE_SIMULATOR_UDID` and `CRAFT_IPAD_SIMULATOR_UDID`; fail if either is missing or fails |
| `package all` | Create distribution-unsigned macOS `.app` + `.zip` (explicit ad-hoc signatures), simulator `.app.zip`, and unsigned iOS-device `.xcarchive`; never export an IPA |
| Scoped build/test/package | Same behavior for that platform only; `test ios-device` is unsupported; build/package support Debug (experimental) and Release (candidate, not release approval) |

The local driver defaults to `build all Debug`; shared CI always supplies all
three arguments, with workflow defaults `phase: build`, `platform: all`,
`configuration: Debug`. `test`/`all` with Release is rejected before native jobs
start; select `build` or `package` for Release candidates.

Drivers must reject invalid arguments, fail nonzero on any failed phase/family,
and must not silently skip missing SDKs/tests/generators/required Rust targets.
`package` must build its own prerequisites: a package-only workflow starts in a
fresh checkout. Likewise, `test` must build prerequisites when needed.
Every invocation must create `target/apple` artifacts or test evidence; absence
is a failure, not a silently empty successful upload.
The shared workflow's `phase: all` expands to applicable build → test → package
calls; it does not pass `all` as a phase to the driver. Its `platform: all` expands
to isolated macOS, Simulator and device **jobs**, omitting unsupported combinations.
No job relies on another job's generated sources or output. iPhone/iPad share one
Simulator job and remain sequential to avoid generated-binding/project races.

The driver owns project/workspace/scheme, bundle names/IDs, pinned generators and
other app dependencies, Rust target selection/linkage, tests using the supplied
simulator UUIDs, actual Xcode arguments and packaging. For example, an ARM64 Simulator
library (`aarch64-apple-ios-sim`) is not the device library (`aarch64-apple-ios`);
macOS universal packaging also needs `x86_64-apple-darwin` plus
`aarch64-apple-darwin`. Do not reuse a library based only on the `arm64` CPU name.
No Swift-only host, PdfCraft name, project path, or proposed `craft-mobile` crate
is embedded in the workflow.

### Unsigned safety and evidence

The workflow uses only `contents: read` (the planning job has no token permissions),
pinned action SHAs matching existing shared workflows, credential-free checkout,
no cache saves, environments or secret inputs, and a 60-minute native-job timeout.
It never invokes certificate import, notarization, App Store Connect or a release
publisher. Uploads are GitHub validation evidence only, not Apple-service uploads.
Do not pass `secrets: inherit`, use `pull_request_target`, or combine
untrusted code with a privileged environment in an app caller.

The workflow supplies `CODE_SIGNING_ALLOWED=NO` and `CODE_SIGNING_REQUIRED=NO`.
**These environment values alone do not guarantee Xcode command-line settings**:
the reviewed driver must pass explicit unsigned settings to `xcodebuild` where
required, never enable automatic provisioning, and never invoke production
packaging/signing scripts that fall back silently. ARM64 macOS execution requires code signatures, so an unsigned-distribution app
may be explicitly **ad-hoc signed**; those signatures are not Developer ID, notarization,
App Store signing or distribution approval. Any such requirement must be explicit
in the app's unsigned build documentation, not reported as signing success.

Package jobs require macOS app/ZIP, simulator app-ZIP, or device archive structure
and reject `.ipa` output.
Those are **structural checks, not signature, Mach-O, installability or App Review
validation**. The app must verify its real products. The iOS `.xcarchive` is an
unsigned engineering artifact, **not an installable IPA**, TestFlight build or
device distribution. A macOS `.zip` is likewise not notarized or Store-ready.
Evidence is tarred before GitHub artifact upload to preserve bundle symlinks and
executable modes, retained for seven days, and uniquely named per phase/platform/
configuration/run attempt. Failed jobs retain available evidence without changing
the original failure into success.

### Hosted runner and simulator selection

On 2026-11-16 GitHub's standard runner reference maps `macos-15` to **ARM64 M1**,
not Intel; the existing desktop release's `macos-15-xlarge` is a different,
larger-runner contract. This POC fixes the standard label instead of accepting an
arbitrary/self-hosted runner input, and fails if the actual host is not ARM64.
Review capacity, 7 GB RAM, disk space, private
repository billing and job timeout before enabling a caller.

`xcode-version` and `rust-toolchain` are required exact numeric versions, with no
`latest`/`stable` fallback. Xcode must already exist at
`/Applications/Xcode_<version>.app/Contents/Developer`; `DEVELOPER_DIR` selects it
without changing global Xcode selection or installing another Xcode. Missing
Xcode/SDK fails clearly. Rust setup uses the existing pinned toolchain action and
installs the four Apple targets listed above; the app retains its lockfile/pins.
The workflow records Xcode/SDK/Rust/Cargo, source SHA and host architecture.

The reviewed `macos-15` image lists Xcode **26.2** with iOS/Simulator SDK **26.2**
(alongside other Xcodes); it does **not** list the local POC's Xcode 27.2.
This is an inventory snapshot, not an assurance for every runner rollout. Confirm
the live image's preinstalled Xcode and compatible runtime before choosing inputs.
No local 27.2 requirement has been downgraded or declared compatible.

An SDK is not a bootable simulator runtime. For Simulator tests, the caller must
set `simulator-runtime` to an exact installed identifier (for example
`com.apple.CoreSimulator.SimRuntime.iOS-26-2`). The shared workflow reads available
runtimes from `xcrun simctl list runtimes --json`, requires that exact available
runtime and its supported device types, then deterministically chooses the first
identifier-sorted iPhone and iPad types. This is a generic family selector, not
an app-owned model registry. It creates distinct job-owned devices, logs the
types/runtime/UUIDs, and exports `CRAFT_IPHONE_SIMULATOR_UDID` and
`CRAFT_IPAD_SIMULATOR_UDID` to the driver. The app runs canonical suites sequentially
using those UUIDs; local users supply their own distinct available UUIDs.

Missing runtime, unsupported type metadata, either missing family, or failed
creation is an explicit failure, never a skipped test. There is no runtime
download, alternate-OS fallback, preexisting device-name assumption or hardcoded
UUID. The shared `always()` cleanup deletes only devices recorded by this job,
including partial creation failures, preserves unrelated devices and reports
cleanup failures. Ownership uses `.craft-apple-simulators.json` at repository root;
drivers must not remove/alter it. Sanitized identities become
`target/apple/ci/simulators.json` evidence. The driver owns suite boot/wait behavior;
shared setup does not boot both devices concurrently. A caller needing an
unavailable Xcode/runtime must wait for a separately reviewed runner/toolchain
change, not fake simulator success.

### Thin caller (after review and merge)

This example deliberately has only `workflow_dispatch`. Resolve the workflow to
an accepted immutable commit before enabling it; the placeholder is not callable.
Do not point an upstream app to this draft fork as if it were accepted.

```yaml
name: Native Apple unsigned validation
on:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  unsigned:
    uses: storytold/craft-common-actions/.github/workflows/apple-unsigned.yml@<reviewed-40-character-commit>
    with:
      phase: all
      platform: all
      configuration: Debug
      xcode-version: "26.2" # Example only: must support the app and installed runtime.
      rust-toolchain: "1.99.0" # Example only: align with the app's reviewed compiler pin.
      simulator-runtime: com.apple.CoreSimulator.SimRuntime.iOS-26-2
      # driver: scripts/build-apple.sh
```

App repositories add/review their driver and a manual caller separately. Later
unprivileged `pull_request` callers may be considered with appropriate budgets.
The shared workflow adds no schedule or caller by itself; existing `gate.yml`,
`release.yml`, signing credentials and artifact collectors are unchanged.
Multiple calls with identical phase/platform/configuration in one workflow run
would collide on artifact names: keep one call per selection/run rather than
adding an arbitrary artifact-name configuration surface.

### Focused validation and remaining proof

`tests/test_apple_unsigned.py` runs the actual YAML shell blocks using the
repository's existing Python `unittest` + PyYAML tooling. Two synthetic consumers
(`tests/apple/first-app/scripts/build-apple.sh` and
`tests/apple/second-app/ci/native-driver.sh`) exercise different app identities
and driver paths. Cases cover selection/scoping, root working directory, ordered
phases, Debug/Release, rejected command strings/traversal, failure propagation,
missing Xcode/SDK, escaping driver symlinks, required package shapes/no IPA,
generic simulator creation/environment handoff, missing runtime/family, partial
creation cleanup, unrelated-device preservation, cleanup error propagation, and
tar mode/symlink preservation. Tool-boundary fixtures are explicitly synthetic:
they do not compile Swift/Rust, run iPhone/iPad tests, validate signing or establish
a successful hosted run. `actionlint` and `shellcheck` cover workflow/fixture syntax.

Before adoption, run the reviewed app driver locally against real Xcode, then a
no-secret hosted manual pilot covering both simulator families and real unsigned
macOS/device products. Record evidence and current limitations. Separate app
tests must establish native correctness, family coverage and binary linkage.

## Future signing/distribution workflows (not implemented)

| Future shared workflow | App caller and trigger | Boundary |
| --- | --- | --- |
| `ios-distribute.yml` with `workflow_call` | New manual `ios-testflight.yml`, on an approved app ref after validation | Protected `ios-testflight` job archives, signs, exports, validates and uploads only the approved build |
| Optional later `ios-appstore-submit.yml` | Separate manual caller and protected `ios-appstore` approval | Submit an already processed, selected build for production review; release remains another explicit decision |

Prefer separate mobile workflows initially: their runner cost, credentials, lifecycle
and review gates differ from desktop GitHub releases. Do not add iOS to the current
`release.yml` schedule, release-branch trigger or draft-release artifact collector.
A later opt-in `has-ios: false` integration could delegate to the accepted reusable
workflow, but only in a separate reviewed CI change with unchanged existing defaults.

The app's approved driver records project/workspace, scheme or equivalent host
adapter, generator versions, device families and simulator selections.
Future distribution additionally identifies a
reviewed commit, team, bundle/app ID, version, unique build number and fixed approved
environment. Signing/distribution inputs remain design decisions, not supported APIs.
Do not accept arbitrary dispatch-supplied shell commands, workflow refs or artifacts
into a privileged job.

Start with bounded manual runs and a measured macOS budget. Unsigned simulator jobs
may cancel superseded validation; signing/upload jobs should serialize per app and
not cancel an upload already in progress.

## Apple membership and identity

Simulator builds do not need a paid membership or a distribution certificate.
A free Xcode Personal Team allows limited, short-lived personal-device testing,
not TestFlight or App Store distribution.

If Storytold does not already have a suitable account, its authorized legal entity
should enroll in the **Apple Developer Program as an organization**, so that entity,
not a contributor, is the store seller. Apple normally requires a legal entity,
D-U-N-S number, binding authority, domain-associated work email and public website.
The Account Holder handles agreements, enrollment/payment and any applicable fee
waiver. This proposal neither verifies existing membership nor enrolls anyone.
The Enterprise Program is not the normal TestFlight/public App Store route.

Human setup, before any signed pilot:

1. Register the official app identifier and required capabilities in Apple Developer
   **Certificates, Identifiers & Profiles** under the chosen team.
2. Create the corresponding app record in **App Store Connect > Apps**. Do not use
   `com.blackoutsecure.pdfcraft.poc` as Storytold's production identity.
3. Create an Apple Distribution signing identity and matching App Store Connect
   provisioning profile. A `.p12` export needs the certificate **and private key**;
   a downloaded `.cer` alone cannot sign.
4. The Account Holder requests API access where necessary. An Account Holder/Admin
   creates a suitably role-limited team API key under **Users and Access >
   Integrations > App Store Connect API**. Keep its `.p8`, key ID and issuer ID.
   The `.p8` authenticates Apple API/upload calls; it does not replace code signing.
5. Grant only the roles each operation needs. Upload, tester management, submission
   and legal/account management need not share one all-powerful credential.
   Team keys apply across apps; GitHub repo restrictions do not narrow Apple's
   permissions. Consider an eligible app-restricted individual user/key if supported
   by the chosen tooling, rather than claiming a team key is app-scoped.

## Where credentials go

**Never publish private signing material or attach it to issues/releases.**
Base64 is only an encoding. Keep offline backups in the organization's secret store.
No Apple account passwords or two-factor codes belong in the repository.

Proposed names, not secrets this proposal creates:

| Value | Location in the app repository |
| --- | --- |
| `IOS_DISTRIBUTION_P12_BASE64` | Settings > Environments > `ios-testflight` > Environment secrets |
| `IOS_DISTRIBUTION_P12_PASSWORD` | Same protected environment secrets |
| `IOS_APPSTORE_PROFILE_BASE64` | Same protected environment secrets |
| `ASC_API_PRIVATE_KEY_P8_BASE64` | Same protected environment secrets; downloaded once from Apple |
| `APPLE_TEAM_ID`, `IOS_BUNDLE_ID`, `ASC_APP_ID`, `ASC_API_KEY_ID`, `ASC_API_ISSUER_ID` | Environment variables; these identifiers are not private keys |

Production submission gets its own `ios-appstore` environment/reviewers and only
the API capability it needs; submitting a previously uploaded build need not
re-import a signing key. Do not duplicate desktop `APPLE_CERTIFICATE` secrets into
iOS blindly: Developer ID macOS notarization and Apple Distribution are different.
Any organization-level secrets must be restricted to the approved caller apps, not
all forks/apps. No app signing credentials belong in `craft-common-actions`.

The three Apple distribution routes must stay distinct:

| Route | Signing and service boundary |
| --- | --- |
| macOS direct download | Developer ID Application (and Developer ID Installer for applicable installer packages), hardened runtime/appropriate entitlements, notarization and stapling; existing desktop release contract |
| iOS/iPadOS TestFlight / App Store | Apple Distribution identity and matching App Store Connect provisioning/profile/entitlements, device archive and signed export, App Store Connect processing, Beta App Review/App Review as applicable |
| Mac App Store | Appropriate Mac App Store distribution signing/provisioning and sandbox/entitlement requirements; Store app record and review, not merely a notarized Developer ID ZIP |

**TestFlight also supports macOS**, not only iOS. A future Mac beta workflow must
use a properly signed/provisioned App Store Connect build, not the existing
Developer ID/ad-hoc DMG or unsigned ZIP. App-specific direct-distribution features
(for example, unsandboxed access or a bundled updater) do not establish Mac App
Store compatibility; a separately reviewed Store profile may be needed. Do not
silently convert the canonical desktop packager to a Store profile.

Shared certificate import's optional-empty/ad-hoc behavior is useful for some
desktop callers but **must not** become success behavior in a protected iOS/Store
job. Reuse reviewed primitives only where identity types and cleanup actually
match; validate each route explicitly. Store eligibility, sandbox/file access and
other app compliance work remain app/maintainer decisions, not guarantees here.

The **job inside the reusable workflow** must declare its protected `environment`;
a caller job using `uses:` cannot declare that keyword. Explicitly map secret names
in the caller's `secrets:` block and declare required secrets in `workflow_call`.
Environment values take precedence, but unpassed secrets can resolve empty.
Check actual nonempty values before signing; declaration alone is insufficient.
Do not use `secrets: inherit`. Agree the environment name/policy rather than allowing
untrusted input to choose an unprotected environment.

Restrict allowed deployment refs and required reviewers, prevent self-approval where
available, and verify these protection features are available/enforced for the repo.
A `workflow_dispatch` button alone is not a security or release-approval boundary.
Signing jobs must never run PR code, `pull_request_target` checkouts, fork artifacts
or arbitrary workflow-run artifacts with privileged secrets. Rebuild the approved
source before exposing credentials, or verify narrowly selected provenance.

The future implementation must address the trust boundary tracked in #7:
compile/test before exposing private keys or upload credentials. Prefer a
keyless build followed by a sign-only job consuming narrowly verified, approved
commit artifacts/provenance; the signing job must not run Cargo build scripts,
proc macros, arbitrary app drivers or untrusted artifact-provided commands with
credentials available. Validate artifact identity and safe extraction. This is
future design work, not an assurance the existing desktop release already has
that separation.

Import keys only into a job-owned temporary keychain on an ephemeral hosted macOS
runner. Decode `.p12`, `.p8` and profiles into permission-restricted temporary files;
install profiles only in the directory expected by the pinned Xcode version.
Generate a per-job keychain password rather than sharing a permanent one. Restore
any changed keychain search list and remove the job's keychain, installed profile
and temporary secret files in `always()` cleanup, including failed exports/uploads.
Never delete unrelated keychains/profiles. Do not cache or upload these resources.
Use bounded artifact retention; sanitized test evidence and upload/build identifiers
are useful, private keys and secret-bearing logs are not.

## TestFlight, review and release are separate hand-offs

1. **Archive/export and upload:** require the matching device Rust release library,
   native archive, bundle/team/profile/entitlement validation, supported Apple SDK,
   version and unique build number. Upload via a maintained Xcode/Transporter/API
   path with documented API-key support; do not assume macOS notarization commands
   or the desktop GitHub Release process distribute an iOS app.
2. **Processing:** report the uploaded build identifier and processing result with
   a bounded wait. An upload success is not a processed or tester-available build.
   Handle duplicates and retryable transport errors without repeatedly uploading.
3. **Private internal testing:** use a named internal TestFlight group, limited to
   authorized App Store Connect users. Group assignment/invitations are explicit
   human approvals. Disable automatic distribution for the pilot group and audit
   existing automatic group assignments before upload; otherwise Apple may
   distribute a build as an upload side effect. Apple currently allows up to 100
   internal testers; builds expire after 90 days. Do not select a TestFlight
   Internal Only build if that build must later reach external testers/customers.
4. **External beta:** invite-only external groups can include people outside the
   team. The first external build requires Beta App Review; later builds may also
   require review. Provide beta information and export compliance first. Public
   TestFlight links are a separate optional decision, not the default for a private
   POC. Apple currently allows up to 10,000 external testers.
5. **Production App Review:** in App Store Connect select the processed build and
   supply current app information, age rating, privacy disclosures/policy, support
   URL, review contact/instructions and iPhone/iPad screenshots. Audit applicable
   privacy manifests/required-reason APIs, SDKs, licensing and export compliance.
   Do not assume an app with cryptography qualifies for an encryption exemption.
   Submit only after maintainer readiness review. Beta approval is not store approval.
6. **Public release:** prefer manual release after Apple's approval for the first
   version; phased release may suit later updates. No workflow should silently
   combine upload, external invitations, review submission and public availability.

An eventual automated review submission is a separate, opt-in phase. Initially use
App Store Connect's UI for submission and release. No secrets need to be configured
merely to review this design.

## Implementation acceptance before enabling any caller

- Preserve existing workflow inputs, defaults, desktop signing and runner costs.
- Add contract/negative tests for opt-in gating, both device families, missing
  secrets/invalid inputs, approved refs, environment binding, least permissions,
  failure propagation, cleanup, and absence of publishing in unsigned/dry-run mode.
- Test export/profile/team/entitlement mismatches, expiration, version/build
  collisions, failed processing and bounded upload retries with non-secret fixtures.
- Run unsigned hosted pilots first. A signed pilot needs maintainer approval and
  real Apple credentials; mock tests are not proof of signing or store acceptance.
- Record physical iPhone/iPad testing, release-profile validation and app metadata
  readiness before broader beta testing. Keep remaining POC limitations visible.
- Review/merge the opt-in unsigned POC in this existing proposal PR, then separate
  app driver/caller PRs. Signing/distribution require a later implementation PR;
  no caller should reference nonexistent `ios-*.yml` workflows.

## Official references checked 2026-11-16

- [Apple enrollment](https://developer.apple.com/help/account/membership/program-enrollment/)
- [Free developer accounts and Personal Teams](https://developer.apple.com/help/account/basics/about-your-developer-account/)
- [App Store Connect API keys and roles](https://developer.apple.com/help/app-store-connect/get-started/app-store-connect-api/)
- [Upload builds and current Xcode requirements](https://developer.apple.com/help/app-store-connect/manage-builds/upload-builds/)
- [TestFlight](https://developer.apple.com/help/app-store-connect/test-a-beta-version/testflight-overview/)
- [Internal testers, automatic distribution and internal-only builds](https://developer.apple.com/help/app-store-connect/test-a-beta-version/add-internal-testers/)
- [Submitting for App Review](https://developer.apple.com/help/app-store-connect/manage-submissions-to-app-review/overview-of-submitting-for-review/)
- [GitHub Apple certificate/profile handling](https://docs.github.com/en/actions/how-tos/deploy/deploy-to-third-party-platforms/sign-xcode-applications)
- [Reusable workflows and environment secrets](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)
- [Deployment environments and concurrency](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments)
- [Hosted runner labels, architectures and limits](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [macos-15 ARM64 Xcode/SDK/runtime inventory](https://github.com/actions/runner-images/blob/main/images/macos/macos-15-arm64-Readme.md)
- [Selecting command-line Xcode tools](https://developer.apple.com/documentation/xcode/selecting-a-default-version-of-xcode)
- [Developer ID versus Store distribution certificates](https://developer.apple.com/help/account/certificates/certificates-overview/)
- [Mac App Store distribution](https://developer.apple.com/help/app-store-connect/manage-builds/upload-builds/)

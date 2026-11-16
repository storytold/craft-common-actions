# Proposal: opt-in Apple mobile validation and distribution

Status: **proposed, not implemented or adopted**, 2026-11-16.
Tracking: [storytold/craft-common-actions#259](https://github.com/storytold/craft-common-actions/issues/259).

This document adds no executable workflow, runner usage, credentials or publishing.
It does not select a suite mobile architecture. PdfCraft's motivating example is an
**unofficial, fork-only iPhone/iPad proof of concept (POC)**, not an official release.

## Existing integration and ownership

Reviewed shared `main` at `59d1ecc4ea15ab59f8d60d38b147d5016c1f1385`.
`release.yml` currently builds/signs/notarizes macOS packages, not iOS apps.
`gate.yml` supplies existing app checks; no iOS workflow is present.
Preserve these contracts and all default runner costs.

| Repository | Responsibility |
| --- | --- |
| `storytold/craft-common-actions` | Reusable toolchain, simulator, signing/export/upload orchestration and its tests |
| Each app repository | Approved native host/build adapter, document engine, app identity, privacy metadata, tests, and thin caller workflows |
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

The [fork-only PdfCraft guide](https://github.com/blackoutsecure/pdfcraft/blob/docs/ios-poc-publishing/apps/ios/PUBLISHING.md)
and [blackoutsecure/pdfcraft#1](https://github.com/blackoutsecure/pdfcraft/pull/1)
record the example's unsupported controls and release blockers. That documentation
PR targets the fork POC branch, not upstream `main`; its latest local workspace/text
enhancements are not included in the published POC baseline or this proposal.

## Suggested workflows, not existing filenames or callable APIs

| Future shared workflow | App caller and trigger | Boundary |
| --- | --- | --- |
| `ios-gate.yml` with `workflow_call` | Start with a new, manual `ios.yml` caller; later consider explicitly opted-in trusted PR checks | No secrets, signing, upload or implicit new macOS matrix in `gate.yml` |
| `ios-distribute.yml` with `workflow_call` | New manual `ios-testflight.yml`, on an approved app ref after validation | Protected `ios-testflight` job archives, signs, exports, validates and uploads only the approved build |
| Optional later `ios-appstore-submit.yml` | Separate manual caller and protected `ios-appstore` approval | Submit an already processed, selected build for production review; release remains another explicit decision |

Prefer separate mobile workflows initially: their runner cost, credentials, lifecycle
and review gates differ from desktop GitHub releases. Do not add iOS to the current
`release.yml` schedule, release-branch trigger or draft-release artifact collector.
A later opt-in `has-ios: false` integration could delegate to the accepted reusable
workflow, but only in a separate reviewed CI change with unchanged existing defaults.

The shared contract should identify project/workspace, scheme or equivalent approved
host adapter, Xcode/SDK, Rust toolchain/targets, generator versions, device families
and simulator runtime/device selections. Distribution additionally identifies a
reviewed commit, team, bundle/app ID, version, unique build number and fixed approved
environment. Exact input names remain a design decision, not a supported interface.
Do not accept arbitrary dispatch-supplied shell commands, workflow refs or artifacts
into a privileged job.

Use the app's reviewed build scripts rather than putting PdfCraft-specific Swift
bindings in this repository. An accepted Rust/egui host may use a different adapter.
The PdfCraft POC uses `bash scripts/test-ios.sh <uuid>` sequentially on iPhone/iPad;
its shared generated output makes concurrent device builds unsafe.
Choose available runtimes/devices explicitly and fail if either family is missing.
Record Xcode/SDK/tool versions and source SHA. The local POC's Xcode 27.2 result
does not establish that a hosted runner has the same toolchain.

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
source in the trusted signing context, or verify narrowly selected provenance.

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
- Land a separate shared CI implementation PR, then separate thin app caller PRs.
  Until then, all names/contracts here are proposals and no caller should reference
  nonexistent `ios-*.yml` workflows.

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

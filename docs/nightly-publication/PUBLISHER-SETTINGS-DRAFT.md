# PhotoCraft publication settings — draft only

No API command in this file has been run. Skylar approved immutable releases and stable-tag protection; the publisher applies and reads back settings before the separately timed first manual publication. Branch pushes, settings/secrets, actual signing and first-run activation are separate public operations, never implied by private tests.

## Immutable releases

For GitHub.com repository `storytold/photocraft`, enable repository release immutability. Current REST endpoint: `PUT /repos/storytold/photocraft/immutable-releases` (no body, Administration write; expected204). Read back `GET /repos/storytold/photocraft/immutable-releases` and record `enabled:true`. Do not use an invented `PATCH /repos/... immutable_releases` field. If organization enforcement applies, record it; do not change organization-wide policy.

Immutability locks published assets and associated tag. Draft→upload all final signed assets→verify→publish remains mandatory. Deleting an immutable release is permitted, then its tag can be deleted; its name cannot be reused. Keep-seven deletion therefore must delete release first and must never recover by reusing a tag name.

## Stable-tag ruleset

Proposed exact body: `photocraft-stable-tags.ruleset.json`, for `POST /repos/storytold/photocraft/rulesets` only if no matching existing rule exists; otherwise preserve/reconcile existing rules and update the exact matching ruleset by ID, never create duplicates blindly. Includes `refs/tags/v*`, excludes only `refs/tags/v*-nightly.*`, blocks update and deletion, active, no bypass actors. This conservatively also protects other non-nightly `v` tags, including release candidates; no creation restriction is added. Do not add the nightly bot as a stable-tag bypass. Check existing release tag conventions and effective organization rules before application. Confirm stable tags are protected and exact producer nightly tags remain deletable after their immutable release is removed.

## Protected signing environment

Callee signing jobs should target the PhotoCraft repository environment `nightly-signing`, not a shared tooling-repository environment. Restrict deployment branches to exact branch `main` (not tags, forks or other branches); enforce literal repository/main/schedule-or-dispatch guards independently in code. Environment secret values may only be referenced/exported in the trusted signing step, never build, arbitrary checkout hooks or downloaded payload execution. No blanket `secrets:inherit` and no logging/exporting key values in outputs, summaries or artifacts.

Before creating/updating environment protection, publisher resolves the actual authorized user/team reviewer IDs with Skylar; this draft deliberately supplies no invented ID. Recommended required reviewer plus prevent self-review, no administrator bypass through the UI where supported. A required reviewer means each scheduled signing run waits for approval; daily trigger timing/approval policy is not silently changed here. REST environment body: `prevent_self_review:true`, `reviewers:[{type:User|Team,id:<resolved numeric ID>}]`, `deployment_branch_policy:{protected_branches:false,custom_branch_policies:true}`; exact branch policy is added separately with `name:main,type:branch`. Read back both environment and branch policy. Do not accidentally leave null/all-branch policy.

Reuse actual existing release secret NAMES, no new parallel credentials:
- Windows PFX route: `WINDOWS_CERTIFICATE`, `WINDOWS_CERTIFICATE_PASSWORD`; or existing Azure route: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_SIGNING_ENDPOINT`, `AZURE_SIGNING_ACCOUNT`, `AZURE_CERT_PROFILE`. App required-mode preflight defines which complete alternative is accepted; incomplete/ambiguous configuration cannot fall back to unsigned.
- macOS: `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `KEYCHAIN_PASSWORD`, `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID` using the existing release import/codesign/notary route. Final TeamID/publisher identity policy must be configured and independently verified.
- Linux: `LINUX_SIGNING_KEY`, the existing ASCII-armored OpenPGP secret signing key. Stable route creates archive`.asc` detached signatures. Consumers need an independently pinned trusted public key/fingerprint; downloading release-signing-key.asc is not trust establishment.

Human publisher configures actual environment secret values without exposing them to agents. Audit metadata/scopes only. Do not delete/move repository-level secrets or break the stable `release` environment/caller while setting up nightlies; any scope migration must be shown and approved with stable-route validation. Protect existing signing routes, clean temporary keychains/key material in finally/always paths and use isolated signing jobs.

## Non-secret reviewed configuration

Before activation, publisher/user must configure these exact values from independently reviewed identities, not from the downloaded feed:
- `NIGHTLY_WINDOWS_PUBLISHER_SUBJECT`: exact Authenticode certificate subject for the allowed publisher (valid OS trust still required; rotating Azure leaf certificates must not silently change the allowed publisher).
- `NIGHTLY_APPLE_TEAM_ID`: exact ten-character Apple TeamID, matching the existing signing account/team.
- `NIGHTLY_LINUX_SIGNING_FINGERPRINT`: full uppercase primary fingerprint, plus reviewed `.github/nightly-linux-signing-key.asc`; Launcher receives an independent reviewed key/fingerprint through its own trusted delivery, never release download discovery.
- `NIGHTLY_REVIEWED_POLICY_SHA256`: canonical digest of independently reviewed `.github/nightly-publication-policy.json` v2 policy. Its baseline must be current main or immediate first parent, fresh within24h, with authenticated complete job/check identities and a retained monotone green-floor review history. Never set it by blindly hashing candidate-controlled policy in the same untrusted run. Manual-input-only skipped jobs and the external Sonar app now have bounded reviewed applicability adapters; the exact fresh policy still needs review and unknown checks remain blocked.
- `NIGHTLY_BOT_ID`: authenticated numeric release bot actor ID, matching the reviewed `.github/nightly-pruning-policy.json` ownership policy. That file additionally binds actual numeric repository/workflow IDs, exact caller workflow/publish job/Publish and Upload nightly ownership receipt step names, reusable tooling workflow path and reviewed historical tooling SHAs. Example IDs in documentation are not deployable values. No caller-controlled body/login/string becomes deletion authority.

Caller-side environment secrets are resolved by the callee’s explicit `environment: nightly-signing`; no blanket secret forwarding is required. Do not assume environment-only values are available to a caller that merely forwards `secrets.NAME`. Keep actual key values scoped to the protected signing step, and verify the protected environment/configuration context on the hosted run.

## First manual run remains off

Resolve real published tooling/caller SHAs, exact reviewed baseline evidence and bot-only ownership policy values; run source-bound hosted checks and signing/native acceptance before enabling publication. Keep source publication, pruning and schedule switches off until Skylar approves exact first-run activation/timing. Before that first activation, authenticated cross-attempt pruning recovery/recheckpointing and a full CLI/checkpoint interruption regression must land and be reviewed; durable plan retention alone does not provide restart recovery. See `../nightly-pruning-draft.md` for the current stop/operator procedure. Once that prerequisite and explicit timing approval are satisfied, keep-seven pruning runs after successful publish with authenticated ownership receipts; no stable/latest update and no broad cleanup. No commands in this draft grant extra authority.

References: https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases ; https://docs.github.com/en/rest/repos/repos ; https://docs.github.com/en/rest/repos/rules ; https://docs.github.com/en/rest/deployments/environments ; https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows . Retrieved2026-11-21; publisher rechecks live schemas/settings before writing.

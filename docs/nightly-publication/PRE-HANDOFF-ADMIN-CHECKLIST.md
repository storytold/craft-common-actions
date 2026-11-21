# Skylar: prepare everything before Brandon's visit

This checklist is a proposal, not a report that settings have been applied.
Use the [full click-by-click guide](SIGNING-ENVIRONMENT-ADMIN-STEPS.md) after
approval of the exact settings/source plan; Brandon only needs the
[one-page credential-owner sheet](CREDENTIAL-OWNER-HANDOFF.md).

- [ ] **Destination ready:** PhotoCraft `nightly-signing` exists; **Selected
  branches and tags → Branch → main** only. Existing `release`, its branch rule,
  secrets and stable release behavior remain unchanged.
- [ ] **Real source protection ready:** protected main + enforced code-owner
  reviews for `.github/workflows/`, relevant packaging/signing code and
  `CODEOWNERS` itself, in the app/shared source as applicable; immutable reviewed
  workflow/action pins. CODEOWNERS alone is not enforcement.
- [ ] **Approval policy explicitly chosen:** required reviewers, prevent
  self-review and no admin bypass are the safer default but require approval
  for each night's signing. Unattended signing is a separate explicit choice
  under enforced source review; this checklist does not silently select it.
  Environment branch rules cannot restrict access to just one job/workflow.
- [ ] **Owner access confirmed:** Brandon can administer environment secrets;
  arrange any necessary org-approved access beforehand. Do not ask him to send
  values to you, and do not try to extract the existing GitHub secret values.
- [ ] **Disposable keychain password prepared:** after settings approval, you can
  create a strong random `KEYCHAIN_PASSWORD` directly in the new environment; it
  is only for the temporary CI keychain, not an Apple private key. Mark it prefilled
  so Brandon does not need to replace it. Never ask him for account passwords.
- [ ] **One visit prepared:** send the exact environment link, the 13-name sheet
  and the observed public-anchor evidence. This deliberately stages Windows,
  Mac and Linux credentials together to avoid a second visit; an admitted main
  job naming the same environment can request all of them after protections.
  Do not claim per-platform isolation or enable an unqualified platform.
- [ ] **Trust work separated:** review public identities independently before
  configuring producer/Launcher anchors; published-key consistency or documented
  Apple Team ID is not native trust/owner confirmation. Keep unknown values closed.
- [ ] **After entry:** read back protection metadata and secret **names only**.
  Keep every caller/publish/prune/schedule guard off. No dispatch just to test
  whether a secret exists. Authenticated recovery must land/review before first
  live publication/pruning; applicable signing/native install QA and separate
  user-approved timing still apply.

**Current backend:** Windows uses the existing six-value Azure client-secret
route, not OIDC. This checklist grants no settings/secret/publication action.

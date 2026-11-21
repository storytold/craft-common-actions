# Signing environments: administrator handoff (draft, no changes applied)

**Recommendation: keep `release` unchanged and provision `nightly-signing`.**
The app lead/requester does not need private keys. A repository administrator
configures protections; the existing credential owner enters values directly.
Azure signing can keep the code-signing private key in Azure. GitHub cannot
reveal existing saved secret values, even to an admin; never add an extraction
workflow or put values in chat, artifacts, logs or a PR.

This is a settings proposal, not permission to apply it. The publisher must show
the exact settings/source changes for approval and read them back afterwards.
Keep nightly publication, pruning and schedules off throughout setup.

## What the current PhotoCraft route actually uses

The publisher's read-only metadata inspection on 2026-11-21 found the `release`
environment restricted to branch `release`, no required reviewers, and admin
bypass allowed. It reported Azure signing names, but no Windows PFX pair.
Verify current metadata before applying any proposal; no secret values were read.

The reviewed shared `release.yml` and
[`actions/setup-windows-signing/sign.ps1`](../../actions/setup-windows-signing/sign.ps1)
use Azure Trusted Signing's pinned client with `AZURE_CLIENT_SECRET`, or a PFX
pair when configured. The nightly signer requires the same complete route.
**Neither this stable backend nor the current nightly adapter implements OIDC.**
Adding a federated credential or omitting the client secret alone will not work.

GitHub environment policies select branches/tags, **not a workflow or job name**.
There is no click that permits “only the nightly signing job on main.” Any
eligible job on an allowed ref that names the environment can request its secrets
once protection rules pass. Required reviewers and enforced workflow review
are necessary additional controls, not a branch-policy workflow allowlist.

## Recommended route: a dedicated `nightly-signing` environment

The exact name is `nightly-signing`, matching the current source; calling it
`nightly` instead requires a separately reviewed source change.

### Repository admin: protections first, secrets second

1. Open **storytold/photocraft → Settings → Environments**. If Settings or its
   controls are unavailable, ask an existing repository admin/org owner; do not
   request anyone's password or signing key.
2. Select **New environment**, enter **nightly-signing**, then **Configure
   environment**. If it already exists, inspect/reconcile it; do not replace it.
3. Under **Deployment branches and tags**, choose **Selected branches and tags**.
   Select **Add deployment branch or tag rule**, ref type **Branch**, pattern
   **main**, and save. Do not add `*`, tags, `release`, or “No restriction.”
4. Under **Deployment protection rules**, enable **Required reviewers**, select
   the actual trusted release-maintainer user/team, and enable **Prevent
   self-review**. Disable **Allow administrators to bypass configured protection
   rules** where supported, then save the protection rules. Do not invent a team
   or silently accept weaker controls if the UI/plan does not support them.
   Required review means scheduled signing waits for approval too; agree that
   operating policy explicitly, rather than promising unattended daily signing.
5. Arrange enforced review for changes to app/shared workflow and signing code:
   protected main, required code-owner review with actual owners for
   `.github/workflows/`, relevant packaging/signing scripts and `CODEOWNERS`
   itself, plus reviewed immutable reusable-workflow/action pins. A CODEOWNERS
   file without an enforcing branch/ruleset review requirement is not sufficient.
   Preserve existing protections; any changes need their own exact approval.
6. Only after those protections are in place, have the credential owner use
   **Environment secrets → Add secret**, enter each exact name/value directly,
   then **Add secret**. The current Windows Azure route needs all six names:
   `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`,
   `AZURE_SIGNING_ENDPOINT`, `AZURE_SIGNING_ACCOUNT`, `AZURE_CERT_PROFILE`.
   The IDs/endpoint/profile are identifiers, but the current adapter reads them
   through the existing secret-shaped interface; do not change that implicitly.
7. For a platform only when its adapter is being qualified, its credential owner
   adds the existing macOS names `APPLE_CERTIFICATE`,
   `APPLE_CERTIFICATE_PASSWORD`, `KEYCHAIN_PASSWORD`, `APPLE_ID`,
   `APPLE_PASSWORD`, `APPLE_TEAM_ID`, or Linux `LINUX_SIGNING_KEY`. Do not
   fabricate/re-export keys or enable unqualified platforms to test the setup.
8. Configure the independently reviewed public identity variables described in
   [`PUBLISHER-SETTINGS-DRAFT.md`](PUBLISHER-SETTINGS-DRAFT.md). The reviewed
   policy digest must be a repository variable available to identity/status/
   publication jobs, not only an environment variable. Signing environment secrets
   remain referenced only by trusted signing steps, never build/payload code.
9. Read back branch policy, reviewer IDs, self-review/bypass settings and secret
   **names only**. Confirm `release` and stable release operation are unchanged.
   Do not dispatch signing just to inspect a secret. The approved hosted test and
   first manual publication are separate operations with native verification.

If the original owner has the saved Azure values in its credential vault, it can
enter them without giving them to the app lead. If nobody has the client-secret
value, the existing Entra application/credential administrator can issue a
separately reviewed, appropriately short-lived nightly credential for the
approved signing identity/profile. Keep the stable credential unchanged until
any separately planned rotation is validated. GitHub repo admin does not imply
Azure credential-administration permission. Prefer the OIDC migration below
when ready; do not pretend it is configured today.

## Alternative: reuse `release` (not recommended for this pilot)

This avoids re-entering existing secret values but expands the stable environment's
eligible refs. Additional protections also affect stable release approvals.
Changing the branch rule alone will not help: the current nightly source names
`nightly-signing`, so a reviewed source patch must select `release` only on the
trusted signing/preflight jobs. Never put a credential-bearing environment on
untrusted builds or add blanket `secrets: inherit`.

If the user explicitly chooses this alternative:

1. An admin opens **Settings → Environments → release** and records the current
   branch/tag rules, protection rules, reviewer/bypass state and secret names.
   Preserve every existing stable-release rule/secret. No values are exported.
2. Before admitting main, set and save required reviewers, prevent self-review,
   disallow admin bypass where supported, and establish enforced code-owner
   review of the app/shared workflow and signing code as above. Agree the effect
   on stable releases; do not silently replace their operating policy.
3. Keep **Selected branches and tags**, retain branch **release**, choose
   **Add deployment branch or tag rule → Branch**, enter exact **main**, and
   save. Never select “No restriction” or a wildcard. Read back both rules.
4. Confirm the exact source patch retains literal repository/main and
   schedule-or-manual-event checks, isolated signing and immutable tooling pins.
   Reviewer approval must inspect the actual workflow/ref/pin/operation; a job
   label saying “sign” is not authority. Another job on main naming `release`
   could otherwise request **all** its secrets, including Apple/Linux credentials,
   not just the Azure values used by the nightly signer.
5. Keep activation off until the approved hosted/native checks. If rejected or
   rolled back, remove only the newly added main rule and reconcile the reviewed
   source/protection changes; do not remove stable rules or rotate/delete keys
   as incidental cleanup.

This route cannot provide job-specific secret isolation through the branch UI.
The dedicated environment limits accidental stable exposure and is our
recommendation; it still needs the review controls above.

## Optional follow-up: Azure OIDC without a stored Windows client secret

OIDC is a **separate reviewed adapter/authentication migration**, not required
private-key access and not a settings-only repair. It would eliminate the
Windows service-principal client secret, not Apple/Linux signing credentials.
A GitHub repo admin and the existing Azure/Entra signing administrator cooperate:

1. In **Microsoft Entra ID → App registrations → the approved nightly signing
   application → Certificates & secrets → Federated credentials → Add credential**,
   choose **GitHub Actions deploying Azure resources**. Select organization
   **storytold**, repository **photocraft**, entity type **Environment**, name
   **nightly-signing**. Verify issuer `https://token.actions.githubusercontent.com`,
   subject `repo:storytold/photocraft:environment:nightly-signing`, and audience
   `api://AzureADTokenExchange` before saving. If the organization customizes
   subject claims, review its actual token/credential contract instead of guessing.
2. In the existing Azure signing account/profile **Access control (IAM)**, have
   the authorized Azure role administrator give that identity only the applicable
   certificate-profile signer role at the narrow supported signing scope, not
   Owner/Contributor across a subscription. Record the actual profile and role;
   the current Microsoft docs use the Artifact Signing service name.
3. Add/review the GitHub adapter change: immutable-pinned OIDC login/signing
   tooling, `id-token: write` only in the isolated trusted Windows signer,
   reviewed non-secret client/tenant/subscription/account/profile configuration,
   and no PFX/client-secret fallback for that OIDC-only route. The current six-value
   preflight/signing backend must be changed and tested; leaving it unchanged
   still requires `AZURE_CLIENT_SECRET`. Do not replace stable signing as a side effect.
4. Preserve native trusted Authenticode, timestamp, exact reviewed full publisher
   subject, unsigned/final receipt and final-byte checks. Review/qualify the
   source-bound hosted route before activation; only then retire any obsolete
   **nightly** client secret in a separately reviewed rotation, never the stable one.

The default environment-based OIDC subject does not itself identify a particular
workflow file. Keep exact main-only deployment policy, required review, protected
workflow source and the isolated job boundary; any stronger workflow-identity
claim needs an explicitly reviewed supported claim/trust configuration.

## References and evidence limits

Read against the actual shared signing source and PhotoCraft
[`docs/releasing.md`](https://github.com/storytold/photocraft/blob/main/docs/releasing.md)
on 2026-11-21. API metadata above was reported by the publisher; no secret values,
settings change, Azure portal operation, real signing or hosted/native test was
performed while writing this guide. Recheck live UI/schema/permissions before use.

- [GitHub environment configuration and permissions](https://docs.github.com/actions/deployment/targeting-different-environments/using-environments-for-deployment)
- [GitHub environment branch policies](https://docs.github.com/en/rest/deployments/branch-policies)
- [Microsoft Entra / GitHub OIDC setup](https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect)
- [Microsoft signing integration](https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations)
- [Official signing action's OIDC guidance](https://github.com/Azure/artifact-signing-action/blob/main/docs/OIDC.md)

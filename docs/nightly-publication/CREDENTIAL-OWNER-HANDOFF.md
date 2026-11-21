# Brandon: PhotoCraft nightly signing, one sitting

**Estimate: 20–30 minutes with the existing vault/portal access.** Send this after
Skylar completes the [separate admin checklist](PRE-HANDOFF-ADMIN-CHECKLIST.md).
You enter every secret **directly into GitHub**; do not give values, private keys,
exports or screenshots to Skylar, an agent, chat, a ticket or a PR. GitHub cannot
read back the existing `release` values. Leave stable `release` unchanged.

1. **Open the prepared destination:** [PhotoCraft environments](https://github.com/storytold/photocraft/settings/environments)
   → **nightly-signing → Environment secrets → Add secret**. Enter the exact
   name/value → **Add secret**, for missing names below (13 in total). Leave any
   reviewed, prefilled temporary-keychain password in place.

2. **Windows — Azure Trusted Signing:** enter `AZURE_TENANT_ID`,
   `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_SIGNING_ENDPOINT`,
   `AZURE_SIGNING_ACCOUNT`, `AZURE_CERT_PROFILE` from the existing approved
   release identity/profile. If the client-secret value is unavailable, open
   [Microsoft Entra](https://entra.microsoft.com/) → **Identity → Applications →
   App registrations → All applications → the existing signing app → Certificates
   & secrets → Client secrets → New client secret**. Description: **PhotoCraft
   nightly**; choose the approved expiry → **Add**. Immediately paste **Value**
   (not Secret ID, shown only once) into GitHub's `AZURE_CLIENT_SECRET` field.
   Record its expiry in your vault. Do not delete/rotate the stable credential,
   create another signing certificate, or change the account/profile permissions.
   Current code requires this client-secret route; OIDC is not implemented yet.

3. **Mac — existing Developer ID/notarization route:** enter `APPLE_CERTIFICATE`
   (base64 `.p12`), `APPLE_CERTIFICATE_PASSWORD` (that export's password),
   `KEYCHAIN_PASSWORD` (a new strong random temporary-CI-keychain password is fine),
   `APPLE_ID`, `APPLE_PASSWORD` (**app-specific** password, not the account password),
   and `APPLE_TEAM_ID`. Use the existing approved release certificate/account;
   no new certificate, ad-hoc signature or private-key transfer to another person.

4. **Linux:** enter `LINUX_SIGNING_KEY`, the existing ASCII-armoured private
   release-signing key in its current noninteractive automation format. Do not
   replace it with the public key or put a private-key export anywhere except
   the GitHub secret field and your approved credential vault.

5. **Confirm public identity only; we have prefilled what we could:**
   - Linux: [published public key](https://github.com/storytold/photocraft/releases/download/v0.23.1/release-signing-key.asc),
     full **primary** fingerprint `2B76741B414AAD6CD33E631213A005C0B9D7C3ED`.
     We found the same key in two releases and verified both checksum signatures;
     please confirm the private key entered above is this intended signing identity.
   - Apple: public release docs say **Learning Machines LLC**, Team ID
     **`DJ6XS33FX8`**. Confirm that this matches the entered certificate/account.
   - Windows: confirm the exact full native `SignerCertificate.Subject`, not just
     its CN/company name. The team is independently checking the shipped stable
     release; do not assume a Subject from another app or a short-lived Azure
     leaf-thumbprint pin. This public value may be shared; private keys may not.

6. **Reply with names/status only:** all 13 names present, any missing item, and
   the client-secret expiry/rotation owner. Stop if an identity or access does
   not match; do not weaken protections or alter stable signing to finish.
   Adding credentials does **not** enable nightlies or admit Mac/Linux. The team
   separately owns reviewed source, public trust configuration, native QA and
   approved first-run timing. Do not dispatch a run or publish a release here.

[Longer settings/security explanation](SIGNING-ENVIRONMENT-ADMIN-STEPS.md) ·
[Microsoft's client-secret UI reference](https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-credentials)

# Required nightly signing (private source draft)

Nothing in this change configures GitHub, uses real signing credentials, runs a
native build, publishes an attestation/release, or enables a schedule. Those
remain publisher/user actions. Historical unsigned Windows proof is not proof of
the final signed bytes. Linux and macOS build callers and native jobs stay
literally disabled. Registry tuples, frozen fixtures and native truth gates are
unchanged.

## Signing boundary

Windows uses two fresh runner jobs. The first compiles without secrets or an
environment, using the confirmed Photo-owned interface:

```
pwsh -File packaging/windows/package.ps1 -Arch x64 -Nightly -BuildOnly
```

It must emit only `photocraft-{version}-windows-x86_64-nightly.unsigned.zip` and
refuse signing credential variables in the compiler environment. Only this new
unsigned upload may be consumed; old 8db/f0 archives are not modified. A separate
protected job checks out exact trusted application source and the defining job's
own immutable shared-tooling SHA, downloads exact identity/build artifact IDs,
and checks independently produced hashes. It bounds and extracts a flat
GUI-only ZIP, calls the actual Photo signing backend:

```
sign.ps1 -Required -CheckOnly
sign.ps1 -Required -Files <stage/photocraft.exe>
```

It never executes that executable. It requires Windows Authenticode status
`Valid`, a timestamp certificate, an exact configured publisher certificate
subject, the exact nightly PE product name/version and x86_64 GUI headers. An
Azure leaf certificate may rotate; there is no permanent leaf thumbprint pin.
The final standard-name ZIP is created only afterward and independently
unpacked/verified again. The unsigned upload is never a publication subject.
The app API changes are owned by Photo and must actually exist in the selected
trusted source; the frozen source inspected here does not yet implement them.

Presence-only protected preflight runs before compilation. It exposes only
boolean secret-presence expressions, not secret values. Actual required-mode
backend preflight and signing run together in the isolated signing step. Build
jobs do not inherit a secret or private keyring from another job.

## Exact configuration draft (publisher/user must apply)

Create an environment named **`nightly-signing` in `storytold/photocraft`**, not
in the shared-actions repository. Restrict deployment branches to literal
`main`, no tags; require the team's designated signing reviewers, prevent
self-review, and disallow administrator bypass where the repository plan
supports it. Protect the main branch/caller/policy and shared immutable pin by
normal human review. Decide reviewer/automation policy before enabling any
schedule: protected configuration and signer jobs can require separate reviews.
Do not remove protection merely to make scheduled runs unattended.

The callee signing job explicitly names this environment. GitHub resolves its
environment secrets there; no blanket `secrets: inherit` and no caller-secret
forwarding is required for this design. See GitHub's reusable-workflow docs,
“Passing inputs and secrets to a reusable workflow”, environment-secret warning.
Actual secret values appear only in each platform's signing step, not workflow
or job env, checkout/build/smoke steps, or downstream publishing jobs.

| Platform | Existing secret names (exact) | Required nonsecret trust policy |
| --- | --- | --- |
| Windows PFX | `WINDOWS_CERTIFICATE` (base64 PFX), `WINDOWS_CERTIFICATE_PASSWORD` | `NIGHTLY_WINDOWS_PUBLISHER_SUBJECT` repository variable, exact full subject accepted by Windows policy |
| Windows alternative | all of `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_SIGNING_ENDPOINT`, `AZURE_SIGNING_ACCOUNT`, `AZURE_CERT_PROFILE` | same exact publisher subject; trust the valid chain, not one rotating leaf |
| macOS | all of `APPLE_CERTIFICATE` (base64 Developer ID P12), `APPLE_CERTIFICATE_PASSWORD`, `KEYCHAIN_PASSWORD`, `APPLE_ID`, `APPLE_PASSWORD` (app-specific), `APPLE_TEAM_ID` | `NIGHTLY_APPLE_TEAM_ID` repository variable; exact ten-character team must match signing configuration |
| Linux | `LINUX_SIGNING_KEY` (existing ASCII-armored, unprotected OpenPGP signing key/subkey) | reviewed app-source `.github/nightly-linux-signing-key.asc`, exactly one primary public key, plus `NIGHTLY_LINUX_SIGNING_FINGERPRINT` repository variable with its independently reviewed full uppercase primary fingerprint |

Repository variables are needed for downstream receipt rechecks outside the
protected environment. Changes require normal reviewed publisher policy. No
actual subject, Apple team, public key, fingerprint, credentials, reviewers or
GitHub settings are supplied by this source draft. Missing policy/configuration
fails closed. A partially configured PFX must not silently select Azure instead.

## macOS aarch64: authored path, not admission

The pending Mac build job remains on the native Arm runner and validates the
pending aarch64 bundle inventory. Before compilation it explicitly requires
actual app-owned `packaging/macos/nightly.sh`, `nightly-sign.sh` and
`import-cert.sh`. Missing adapters fail closed. The proposed app interface is:

```
bash packaging/macos/nightly.sh --arch aarch64
bash packaging/macos/nightly-sign.sh --arch aarch64 --input <unsigned.zip> --output <final.zip>
```

The build produces a GUI-only `PhotoCraft Nightly.app` ZIP without secrets. The
sign adapter must reuse the stable `import-cert.sh` certificate import and
release `codesign` hardened-runtime/secure-timestamp, `notarytool submit --wait`
(Accepted only), and `stapler staple` route, staple the app *before* final ZIP,
and clean up its temporary keychain on every exit. It must never run cargo,
package hooks or downloaded executable code with credentials available.

Do **not** invoke the existing stable `package.sh`/`verify.sh` with signing keys:
those scripts execute bundled CLI code and are not this isolation boundary.
The shared clean job independently verifies the final ZIP's exact thin arm64
entrypoint, nightly bundle/plist identity, Developer ID authority, configured
team, hardened runtime, secure timestamp, signature integrity, stapled ticket
and Gatekeeper assessment without executing payload. Ad-hoc, missing signatures
or notarization never fall back to success.

The actual Photo nightly Mac adapter and native proof are incomplete. The old
`nightly_unix.py require_build_policy` Mac refusal deliberately remains too;
removing it is a later source/native policy decision, not a result of authoring
signer YAML. No macOS tuple is admitted or staged into the current feed.

## Linux: use the stable OpenPGP scheme, not a new one

Inspected stable shared-actions checkout:
`c8f4b6ec2a894285213c341611b6b023b0b3e469`,
`actions/draft-release/sign-linux.sh`, `actions/import-signing-key/action.yml`,
`actions/draft-release/action.yml`, and README lines 100–116.

Stable `sign-linux.sh` signs other `*-linux-*` files using
`gpg --batch --yes --local-user "$SIGNING_FPR" --armor --detach-sign -o "$f.asc" "$f"`
and verifies via `gpgv`. It exports `release-signing-key.asc`; draft-release
also signs `SHA256SUMS.txt.asc`. README says `.tar.gz` uses `<file>.asc` and:

> The release also carries the public key as `release-signing-key.asc`; publish
> its fingerprint somewhere users can check it independently. On first install,
> compare that fingerprint with the key in the bundle: a bundle can contain an
> attacker-controlled key.

The pending isolated nightly job uses that existing `LINUX_SIGNING_KEY` and
ASCII-armored detached `<exact nightly tar.gz>.asc` signature. Tar bytes do not
change; the signature and exported public key are required support files. A
private temporary keyring imports the secret; verification uses a separate
public-only keyring constructed from the reviewed app-source public key, never
the downloaded release key. The current helper signs only with that reviewed
primary key (GnuPG may select its signing subkey) and re-verifies the result.
It never runs or extracts the payload in the signing step. Temporary keyrings
are removed on success/failure; private keys are never exported/uploaded.

**Launcher contract proposal, still a blocker:** authenticate the full public
primary key/fingerprint independently in reviewed Launcher source/configuration;
require a valid OpenPGP detached signature over exact downloaded tar.gz before
unpacking/installing/executing; reject missing, altered, unknown-key or revoked/
expired-policy signatures. Never bootstrap trust from `release-signing-key.asc`
in the same release/feed, even if a manifest hash agrees. Signature asset name,
size and hash must be bound by agreed `craft-nightly/v2` artifact.signature Download metadata (same release, exact `<archive>.asc`, 16 KiB cap), checksum lines and release asset digest, with verification before unpack/swap. No v1/debug fallback is permitted.
Rotation needs a separately reviewed trust update, not a feed-supplied new key.
The current feed schema and Launcher do not implement this contract; Linux
remains unadmitted, with no claim of signed-install or native proof. A future
Linux publisher must carry/verify the signature support and any signed checksum
metadata too, not just reuse the current Windows-only three-file packet.

## Transport, staging and attestation

Internal `craft-nightly-signing/v1` receipts are provenance, not cryptographic
signature substitutes or a new public client signature schema. They bind the
original immutable identity hash, original unsigned build hash, final signed
package hash, source commit, defining tooling revision, original run/attempt,
asset, platform/method and verified signer. Linux also binds detached-signature
and public-export file hashes/sizes. The receipt has its own exact upload ID and
independent signer-step SHA256.

Windows stage, attestation and publish jobs each download that original receipt
by exact ID and recheck its independent hash plus all original/final identity
bindings and publisher policy. Existing finite packet/hash checks still bind
final signed ZIP bytes. Attestation subjects reference the signed producer's
final hash, **not** the unsigned build hash or an old native proof. Disabled
attestation/publication literals stay off. No same-name artifact substitution,
self-consistent downloaded receipt, optional signing or unsigned fallback is
accepted. `verify-receipt --package-directory ... --source ...` also rechecks
required final/support files; Linux re-verifies against source-reviewed public
trust, not the exported artifact key.

Tests use synthetic PE/archive bytes, fake API fixtures and mocked external
processes. They prove source boundaries/refusals and byte binding, not Windows
Authenticode, Apple signing/notarization, OpenPGP with production keys, GitHub
artifact/environments/attestation semantics, configured identities, or native
Launcher install behavior. Those require separately authorized real runs.

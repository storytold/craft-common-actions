# Private first nightly slice

This source successor admits exactly PhotoCraft Windows x86_64 portable ZIP.
All other application target arrays remain empty. Prerelease publication is
hard-disabled for E1-E5 by literal `if: ${{ false }}` in nightly.yml.

The new reusable nightly-windows.yml extracts the existing Windows setup but
uses the app-owned `packaging/windows/package.ps1 -Arch x64 -Nightly` entry.
PHOTOCRAFT_BUILD_CHANNEL=nightly-v1 and the validated prepared identity version
are explicit build-step env; the script owns the locked HEIF-enabled compiler,
GUI-only packaging, licenses and flavor checks. No stable dry-run ZIP is used.
The existing release.yml is byte-identical. When the source/tooling/stable-base
key changes or manual force is requested, its full diagnostic dry-run still runs
for Photo (including MSI/other-OS release-breakage guards). Unchanged-source skip
covers both diagnostics and native packages; the isolated nightly build is
additional, never a replacement. Status reports
both results and requires diagnostic success plus registered-nightly success.
No stable release mode is changed.

Staging only downloads this run's exact dedicated nightly Windows artifact.
nightly_feed.build_feed and stage_feed are local filesystem-only functions.
NIGHTLY.json keeps canonical GitHub URLs; local staging does not establish that
those URLs, tags, releases or binaries exist on GitHub. E4 consumes the SAME E3
ZIP and prepared identity; synthetic ZIP tests are not runtime qualification.

Do not dispatch, publish, push or enable live release from this source packet.
E3/E5 native package/profile/IPC/updater checks and independent source review are
required; E6 additionally requires explicit user approval through the publisher.

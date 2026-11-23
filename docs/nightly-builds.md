# Private first nightly slice

This source successor admits exactly PhotoCraft Windows x86_64 portable ZIP.
All other application target arrays remain empty. Prerelease publication is
hard-disabled for E1-E5 by literal `if: ${{ false }}` in nightly.yml.

The new reusable nightly-windows.yml extracts the existing Windows setup but
uses the app-owned `packaging/windows/package.ps1 -Arch x64 -Nightly` entry.
PHOTOCRAFT_BUILD_CHANNEL=nightly-v1 and the validated prepared identity version
are explicit build-step env; the script owns the locked HEIF-enabled compiler,
GUI-only packaging, licenses and flavor checks. No stable dry-run ZIP is used.
The existing release.yml is byte-identical. The installable producer never calls
it or combines diagnostic results with its status. The independent
`nightly-diagnostics.yml` workflow currently validates metadata only: its legacy
packaging call is paused because Flatpak packaging would otherwise receive a
package-write token even during a dry run. MSI/other-OS legacy packaging coverage
is not provided by this metadata-only workflow. Restoring it requires a separate
read-only packaging/privileged publication split in the stable workflow.
Its result never feeds the producer, but under a v2 publication policy its check
runs are not independent of the publication gate: the gate refuses any check on
the candidate or baseline commit that is neither a reviewed required check nor
part of the producer run. Dispatching diagnostics on main's current or previous
commit therefore stops that nightly (fail-closed) until main advances. No
authenticated diagnostic exemption exists yet.
Unchanged-source skip applies only to the installable producer. No stable release
mode is changed.

Staging only downloads this run's exact dedicated nightly Windows artifact.
nightly_feed.build_feed and stage_feed are local filesystem-only functions.
NIGHTLY.json keeps canonical GitHub URLs; local staging does not establish that
those URLs, tags, releases or binaries exist on GitHub. E4 consumes the SAME E3
ZIP and prepared identity; synthetic ZIP tests are not runtime qualification.

Do not dispatch, publish, push or enable live release from this source packet.
E3/E5 native package/profile/IPC/updater checks and independent source review are
required; E6 additionally requires explicit user approval through the publisher.

# Launcher candidate qualification

`release.yml` accepts `launcher-qualification: true` for `storytold/craft-launcher`. Other apps
keep the default `false`, so this adds no builds, runner usage or new gate to their releases.

The opt-in reusable job depends on the existing macOS, Windows and Linux packaging jobs. It
downloads their immutable artifacts from the same run and executes the launcher's
`packaging/e2e/runner.py` on seven native architecture targets. It does not rebuild binaries or
receive signing secrets. Linux gets a virtual desktop and real package tools. The Windows ARM
job runs on Windows ARM, while x86 runs as a 32-bit process on the x64 image.

Draft creation requires this job's explicit `success`; skipped, cancelled or failed qualification
cannot pass. Before creating, editing, deleting or uploading any GitHub release asset,
`actions/draft-release` downloads the reports and runs the exact source commit's
`packaging/e2e/verify_release.py`. That validator checks the required platform/operation set,
source SHA, workflow run, version, cleanup/restart evidence and artifact hashes. The generated
`launcher-qualification.json` joins the candidate's release assets/checksums.

`create-draft: false` skips the release job entirely. The qualification workflow's separate
read-only verification job still checks the native result files, GUI records, updater record,
summary, and artifact hashes, then uploads `launcher-candidate-evidence`. It has no release action
or write token. The launcher caller defaults manual dispatch to this mode; an explicit opt-in or
a release-branch push can create a draft only after qualification. Existing app callers retain
the previous `create-draft: true` default. No job here publishes a draft.

The companion `launcher-qualification-contract.yml` is a cheap PR gate for the launcher's
Python evidence-contract tests. The ordinary launcher Rust jobs already compile/test its native
qualification module, so no extra full PR matrix is added. Full qualification runs only when
the existing candidate/release workflow is invoked. No scheduled workflow is added.

This repository owns the existing build fan-out and sole draft-release collector; therefore the
dependency/gate and pre-upload byte comparison belong here. Product-specific fixtures, native
operations, runner and evidence schema stay in `craft-launcher`. This avoids duplicating the
release pipeline in the launcher or changing the release behavior of every other application.

Land the shared opt-in interfaces before enabling them in the launcher caller. Keep the launcher
integration branch separate from its verifier hotfix. Validation of these local changes does not
mean hosted candidate execution has passed. `tests/test_launcher_qualification.py` checks the
workflow contract; the launcher README and generated evidence enumerate remaining UI,
format/architecture, self-update-discovery and disabled-Gatekeeper coverage boundaries.

After both branches and the verifier hotfix are integrated, authorize a manual launcher run on
the reviewed SHA with `create-draft: false`. Review all seven target results and the combined
evidence before using an explicit draft-enabled release run. That release run qualifies its own
bytes; it must not reuse staging evidence for a rebuild. Nothing should be pushed or dispatched
merely to test the local workflow syntax. Local validation is not hosted execution evidence.

This gates this workflow's draft creation; it does not alter repository protections or prevent
a maintainer manually publishing a release outside the workflow.

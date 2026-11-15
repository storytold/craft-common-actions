# Spec: CI optimisation across the artcraft suite

- Status: draft, for discussion
- Date: 2026-11-15
- Plan: [docs/plans/ci-optimisation.md](../plans/ci-optimisation.md)
- Decision records: [ADR 0002, CI runs in three tiers](../adr/0002-ci-runs-in-three-tiers.md);
  [ADR 0003, app CI lives in craft-common-actions](../adr/0003-app-ci-lives-in-craft-common-actions.md)

## Problem

In the week of 8–14 Nov 2026, the storytold org used at least **1.40M Linux-equivalent Actions
minutes** (Windows counted at 2×, macOS at 10×) over 65,767 workflow runs in 46 repositories.

- 82% of runs were triggered by pull requests.
- The heavy repositories are public, so standard runners cost no money. The cost is time: every job
  queues behind the org-wide limit on concurrent jobs, and a photocraft PR waits about 45 minutes for
  its gate.
- The org is moving to its own runners. That adds capacity, but it doesn't remove duplicated or
  unnecessary work.
- **No app has an owner.** Fixes have to be designed so that one central change, or one scripted
  change applied to every app, does the job, and so that the suite can't drift back afterwards.

### Where the minutes go

| Area | Linux-eq min / week | Notes |
|---|---|---|
| App CI gates (`gate.yml` plus app jobs) | ~766k (55%) | photocraft/CI alone is 29% |
| SonarCloud | ~208k (15%) | Re-runs the whole suite with coverage on every PR |
| wordcraft Printing | ~161k (11.5%) | macOS leg at 10× on PRs |
| Other macOS and Windows legs | ~100k | effectcraft, anicraft, craft-ai-integrations, craft-libs |
| FreeBSD, ARM64, Nix, packaging, docs | the rest | Mostly scheduled or path-filtered already |

The API returns at most 1,000 runs per query, and the busiest repositories hit that cap on several
days. These figures are a lower bound.

### Root causes found

| ID | Cause | Evidence |
|---|---|---|
| RC1 | Every push to a PR runs the full suite | wordcraft ran about 13 PR gate runs per merge. photocraft and wordcraft each have more than 1,000 open PRs, mostly agent-opened (`fix/`, `feat/`, `codex/`). |
| RC2 | The same tests run more than once per PR | SonarCloud re-runs the suite. photocraft runs its 1,691-test GPU group twice: once in the main run and again on lavapipe (about 1,150 test-seconds each time). |
| RC3 | Expensive platforms run on PRs | macOS legs (10×) and Windows legs (2×) on ordinary PRs. |
| RC4 | Our own crates are rebuilt every run | rust-cache keeps dependencies only. Building test binaries takes 15–30 min even on a cache hit (vectorcraft: 30 crates in 29 min; photocraft: 106 test binaries in 16 min). `sccache` is available in `gate.yml` but off by default. |
| RC5 | Cache bugs | (a) A failed main run saved a half-filled cache that rust-cache never replaced, so 3 of 6 photocraft and 3 of 7 wordcraft gates rebuilt 370–470 dependencies (+6–9 min). (b) photocraft's Windows `CARGO_TARGET_DIR` made rust-cache miss everything after 13 Nov (+about 47 min per Windows run). (c) Environment variables leaked by `cargo run` (from `cargo xtask`) rebuild ring and its dependents whenever cargo is called outside xtask (+2.3 min per photocraft gate). (d) Every heavy repository is at or over the 10 GB cache limit, with FreeBSD entries of 2–7 GB. |
| RC6 | A slow tail of tests | 933 tests averaging over 2 s take 55% of all test time across six apps (about 69k tests). Examples: vectorcraft's `every_command_with_junk_params` (256 s), fuzz-style "never panics" tests, linear-time guards, and the same check repeated at several sizes. Tests under 0.1 s are 10.5% of the time. |
| RC7 | Apps own their CI | Apps call shared workflows but keep their own `ci.yml`, `xtask` and `nextest.toml`. photocraft defines 15 jobs of its own. A suite-wide fix needs a change in each app unless the behaviour moves into common-actions. |
| RC8 | Nothing measures CI | The numbers above came from a one-off analysis. Nobody would notice a regression like RC5(b). |

## Goals

| ID | Goal | Target |
|---|---|---|
| G1 | Cut suite-wide Actions minutes | At least 45% below the baseline of 1.40M per week |
| G2 | Shorten PR feedback | Median PR gate on the five heaviest apps at or under 20 min (photocraft today: about 45) |
| G3 | Keep main as safe as today | Full suite, all platforms and coverage still run on every merge to main |
| G4 | Make it hold without app owners | Every rule is enforced by a shared workflow or check, and CI regressions are visible every week |
| G5 | Be ready for the org's own runners | Moving to them is a change of inputs, not a rewrite |

## Non-goals

- Larger GitHub-hosted runners. They are billed even on public repositories, and the org's own
  runners replace them.
- Replacing rust-cache with `actions/cache`. Both use the same storage, and there's nothing to gain.
  rust-cache goes when the org's own runners keep a warm `target/`.
- Moving i18n reports. They take under 1 s, except wordcraft's audit, which is a real check. They
  stay as they are.
- Deciding which tests are useless. This spec moves slow tests between tiers by rule. Deleting a
  test stays a code-review decision.

## Requirements

Each requirement has an ID that the plan's tasks and the verification section refer to.

### Measure

- **REQ-M1 Weekly CI report.** A scheduled workflow in craft-common-actions produces Linux-equivalent
  minutes per repository and per workflow, job-level averages, queue times, and cache usage per
  repository. It publishes a week-on-week comparison. It replaces the one-off scripts used for this
  spec.
- **REQ-M2 Test timings.** Every gate run uploads nextest's JUnit output. The weekly report lists the
  slowest 100 tests in each app, and any test that got more than twice as slow.

### Stop waste (shared fixes)

- **REQ-W1** Never save a dependency cache from a failed job (RC5a). *Done in #253.*
- **REQ-W2** The shared rust-cache action reaches a `CARGO_TARGET_DIR` outside the checkout (RC5b).
  *Done in #253.* photocraft's own Windows job is fixed by storytold/photocraft#15234.
- **REQ-W3** Cargo started by an app's `xtask` sees the same environment as cargo started directly,
  so nothing rebuilds between the two (RC5c).
- **REQ-W4** Each repository's steady-state cache stays at or under 8 GB, and FreeBSD caches are
  capped (RC5d).

### Run less on PRs (ADR 0002)

- **REQ-T1 Three tiers.** Every check belongs to exactly one tier:
  - **PR:** fmt, clippy, the affected tests on one Linux leg, and cheap policy checks.
  - **Main:** the full suite, Windows and macOS legs, SonarCloud, the GPU group on lavapipe. Runs on
    every push to main, or in a merge queue once that's on.
  - **Nightly:** fuzzing, timing guards, every-size and exhaustive sweeps, FreeBSD, perf.

  The `full-ci` label promotes a PR to the main tier.
- **REQ-T2 Affected tests.** On PRs, the gate runs only tests in packages the change touched, plus
  every package that depends on them, through a nextest filter. Changes to the lockfile, root
  manifest, toolchain, Cargo or nextest configuration, xtask, workflows, or files outside a package
  run everything. Selecting packages with `-p` is not used, because it changes feature unification
  and misses the cache.
- **REQ-T3 Drafts are not skipped.** Draft PRs get the PR tier like any other PR. That's a
  deliberate decision (15 Nov): authors and agents rely on CI feedback while a PR is still a draft.
- **REQ-T4 SonarCloud** runs in the main tier only, or consumes coverage the main-tier gate already
  produced, so tests never run twice for coverage.
- **REQ-T5 macOS and Windows** legs run in the main tier, plus on PRs whose paths match the
  platform-specific code (the `changed-paths.yml` pattern photocraft already uses for Windows).
- **REQ-T6 Merge queue.** Repositories with more than 100 merges a week turn on GitHub merge queue,
  with the main tier as the queue's check.
- **REQ-T7 Agent PR hygiene.** Agent tooling batches pushes, and PRs with no activity for 14 days
  are closed automatically. The suite's AGENTS.md house rules (craft-contrib)
  carry the rule, so every agent sees it.

### Remove duplicate work

- **REQ-D1 GPU tests once.** `gate.yml` gains a `gpu-group` input. The main test run leaves that
  nextest group out, and the gate runs it once on lavapipe, checking that nothing skipped silently.
  This replaces photocraft's command chain.
- **REQ-D2 Coverage once.** See REQ-T4.

### Make builds and tests cheaper

- **REQ-B1 sccache on Linux.** The gate turns `sccache` on by default for Linux legs, read-only on
  PRs, so our own library crates aren't rebuilt from scratch. Windows waits for an sccache release
  that includes mozilla/sccache#3026 (merged 14 Nov, not yet released). `gate.yml` pins v0.18.0,
  which has the bug.
- **REQ-B2 Test build size.** Apps keep the test profile's debug info at `line-tables-only` (the
  gate's default) and merge integration-test binaries where a crate has many. Every test binary
  costs a link.
- **REQ-B3 Test tiers by convention.** A test that belongs to the nightly tier is marked
  `#[ignore = "nightly: <reason>"]`. The PR and main tiers skip ignored tests. The nightly tier runs
  them with `--run-ignored only`. Candidates come from REQ-M2 and from the patterns in RC6:
  - fuzz-style "junk input" tests,
  - linear-time or bounded-time guards,
  - every-size variants beyond one representative size,
  - exhaustive "every X" sweeps.
- **REQ-B4 Slow-test budget.** The PR tier's nextest profile warns at 10 s, and the weekly report
  lists every test over 30 s. A *new* test over 30 s fails the house-rules check unless it is marked
  for nightly.

### Hold the line (ADR 0003)

- **REQ-G1 App CI lives in common-actions.** App workflows only call shared workflows. App-specific
  behaviour becomes an input. Today's app-local jobs (photocraft has 15, most apps 0–2) move into
  shared workflows or are deleted.
- **REQ-G2 CI contract check.** A shared workflow, run like `house-rules.yml`, fails an app whose
  workflows break the contract: a local job that isn't on the allowlist, a missing
  a missing `concurrency` cancel on PRs, or a macOS leg on PRs without a path
  filter.
- **REQ-G3 Suite sweep tool.** A script in craft-common-actions applies a mechanical change to every
  app repository and opens one PR per app. Without app owners, the central team rolls changes out
  with it.

### Own runners

- **REQ-R1** `gate.yml` and the other shared workflows take runner labels as inputs everywhere, so
  moving to the org's own runners is a change of inputs.
- **REQ-R2** PRs from forks never run on the org's runners. The heavy repositories are public, and
  anyone can open a PR. Fork PRs stay on GitHub-hosted runners or on throwaway VMs.
- **REQ-R3** On the org's runners, rust-cache and the GitHub cache are replaced by a warm local
  `target/` and a local sccache.

## Estimated savings

The rows overlap, so the total is lower than their sum.

| Workstream | Linux-eq min / week |
|---|---|
| Stop waste (REQ-W1–W4) | 20–40k |
| Avoided regression: photocraft Windows rust-cache (#15234) | ~100k+ since 13 Nov (not in the baseline) |
| Affected tests (REQ-T2) | 60–120k |
| Agent PR hygiene: batched pushes, stale PRs closed (REQ-T7) | Not estimated; tracked by runs per merge in REQ-M1 |
| SonarCloud on main only, or coverage reuse (REQ-T4) | ~190k |
| macOS and Windows off ordinary PRs (REQ-T5) | ~200k |
| Merge queue (REQ-T6) | Multiplies T2; not estimated |
| GPU group once, no rebuild (REQ-D1, W3) | 12–18k |
| sccache, nightly tier for slow tests (REQ-B1, B3) | 50–150k (to be measured in a pilot) |
| **Total** | **~580–700k a week, about 40–50%** |

## Risks

| Risk | Requirements | Mitigation |
|---|---|---|
| A break the PR tier missed reaches main | T1, T2, B3 | Main runs the full suite on every merge. The merge queue (T6) catches breaks before merge. `full-ci` gives a manual escape hatch. The fallback rules in T2 err towards running everything. |
| A test marked nightly hides a real regression for up to a day | B3 | The nightly tier fails loudly into the weekly report. Marking needs a reason string, and the house-rules check reviews new marks. |
| Moving app jobs into common-actions makes the shared workflows complex | G1 | Each moved behaviour is an input with a default that changes nothing. Behaviour that only one app needs stays an app input, not a branch inside the workflow. |
| Fork PRs run untrusted code on the org's runners | R2 | Fork PRs never use those runners. |
| sccache serves a stale compile | B1 | It keys on compiler arguments and inputs. `gate.yml` already resets its cache when `Cargo.toml`, `build.rs` or `cache-version` changes. On PRs it is read-only. |
| One central team changing ~40 repositories | G3 | The sweep tool makes each change mechanical, and the contract check proves it landed. |

## Verification

| Requirement | Verified by |
|---|---|
| M1, M2 | The weekly report runs on schedule. Its totals agree within 10% with a manual run of the baseline scripts for the same week. |
| W1, W2 | `tests/test_rust_cache.py` (in #253). After merge, main logs `Cache saved` on photocraft's Windows integration leg. |
| W3 | In a photocraft gate log, nothing compiles between `CI summary` and the lavapipe tests. |
| W4 | REQ-M1 shows each repository at or under 8 GB for two consecutive weeks. |
| T1–T5 | Unit tests for every new shared input. Pilot apps' PR logs show the tier and the test selection, and their main runs show the full suite. |
| T6, T7 | Merge queue enabled where REQ-T6 applies. Open PRs per repository and runs per merge, from REQ-M1, go down. |
| D1 | A photocraft gate log shows the GPU group once, with the silent-skip check passing. |
| B1–B4 | Pilot before and after gate times from REQ-M1. The house-rules check rejects a sample new 31 s test that isn't marked. |
| G1–G3 | The contract check passes on every app. |
| R1–R3 | One app's gate runs on an org runner using inputs only. A fork PR is shown to stay on hosted runners. |
| G1 (goal) | REQ-M1 shows at least 45% below baseline four weeks after phase 3 completes. |

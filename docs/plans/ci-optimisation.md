# Implementation plan: CI optimisation across the artcraft suite

- Spec: [docs/specs/ci-optimisation.md](../specs/ci-optimisation.md)
- Date: 2026-11-15
- Owner: the central CI team (until the apps have owners)

## How the work is shaped

No app has an owner, so every change is one of three kinds:

1. **Shared:** a change in craft-common-actions. Apps call shared workflows at `@main`, so it reaches
   every app at once. Each new behaviour is an input whose default changes nothing, until the
   rollout step turns it on.
2. **Sweep:** the same mechanical change in every app, opened as one PR per app by the sweep tool
   (task P0.3) and merged by the central team.
3. **Pilot:** a shared change turned on for one or two apps first, measured with the weekly report,
   then swept.

The pilot apps are **vectorcraft**, the simplest heavy app (one gate job), and **photocraft**, the
heaviest, with the most app-local CI. A change that works in both is safe to sweep.

## Phase 0: measure and enable (week 1)

| Task | Requirement | Kind | Change | Done when |
|---|---|---|---|---|
| P0.1 | M1 | Shared | `ci-report.yml`: a weekly scheduled workflow that turns the baseline analysis (runs, sampled jobs, multipliers, cache usage) into a script under `scripts/`, then publishes a report artifact and a summary issue. | The first report matches the baseline within 10%. |
| P0.2 | M2 | Shared | The gate uploads nextest JUnit output (the `ci` profile's `junit` path) as an artifact. The report collects the slowest 100 tests in each app and anything that got more than twice as slow. | The report lists slow tests for the pilot apps. |
| P0.3 | G3 | Shared | `scripts/sweep.py`: clones each app repository listed in a manifest, applies a named change (a Python function), and opens a draft PR per app with a standard body. It supports a dry run. | A dry run against every app prints the diffs. |
| P0.4 | W1, W2 | Shared | Merge #253 (no cache save on failure; `CARGO_TARGET_DIR` reachable). | Merged. A green main saves normal-size entries. |
| P0.5 | (regression) | App | Merge storytold/photocraft#15234 (Windows rust-cache), preferably with `cache-on-failure: false`. | photocraft's Windows integration leg on main logs `Cache saved`. |

## Phase 1: stop waste and set up the tiers (weeks 1–2)

| Task | Requirement | Kind | Change | Done when |
|---|---|---|---|---|
| P1.1 | W3 | Sweep | Each app's `xtask` `cargo()` helper removes the environment variables `cargo run` sets for xtask (`CARGO_PKG_*`, `CARGO_MANIFEST_DIR`, `CARGO_MANIFEST_PATH`, `CARGO_CRATE_NAME`, `CARGO_BIN_NAME`, `CARGO_PRIMARY_PACKAGE`). | photocraft's gate shows no rebuild after `CI summary`. |
| P1.2 | W4 | Shared, then per repository | Cap the FreeBSD cargo cache (one entry per architecture on main, pruned before save). Point app jobs that build the gate's workspace at `job: gate`. Use P0.1's data per repository. | Every repository at or under 8 GB for two weeks. |
| P1.3 | T3 | Sweep | Add `ready_for_review` to every app's `pull_request` types. | The contract check (P4.2) or a sweep dry run shows every app done. |
| P1.4 | T3 | Shared | `gate.yml` (and the other shared PR workflows) skip their jobs on draft PRs. Ships only after P1.3 is everywhere. | A draft PR in a pilot app starts no gate. Marking it ready runs it. |
| P1.5 | T1 | Shared | A `tier` output computed once per run (`pr`, `main` or `nightly`, from the event and the `full-ci` label) in a small shared workflow every app calls first. Later tasks key on it. | Unit tests cover each event and label. |
| P1.6 | T7 | craft-contrib, agent configuration | Add to the AGENTS.md house rules: open PRs as drafts, push in batches. Add a scheduled stale-PR closer (14 days of inactivity) to the shared workflows. | Open PRs per repository trend down in P0.1. |

## Phase 2: run less on PRs (weeks 2–4)

| Task | Requirement | Kind | Change | Done when |
|---|---|---|---|---|
| P2.1 | T2 | Shared | `actions/affected-tests`: reads the PR's merge commit against its first parent (as `changed-paths.yml` does), maps the changes to workspace packages through `cargo metadata --no-deps`, and applies the full-suite rules. It exports `CRAFT_TEST_FILTER`, a nextest filter such as `rdeps(=pkg-a) \| rdeps(=pkg-b)`, or nothing for a full run, and logs the reason. Plus a `gate.yml` input `test-affected`, with tests. | Unit tests cover a changed crate, a leaf crate, the root manifest, a file outside any package, and a missing diff. |
| P2.2 | T2 | Pilot, then sweep | Each app's `xtask` adds `-E "$CRAFT_TEST_FILTER"` and `--no-tests=warn` to its nextest step when the variable is set, combined with any filter it already uses. Turn on `test-affected` in vectorcraft and photocraft for two weeks. | Pilot PR gates show the selection. No main-only failure was missed for lack of a fallback rule. P0.1 shows the saving. |
| P2.3 | T4, D2 | Shared | `sonarcloud.yml` runs on the main tier only. Option B, to be decided in the P2 review: `gate.yml` produces LCOV through `cargo llvm-cov nextest` on main and Sonar consumes it, so tests never run twice. | SonarCloud minutes on PRs reach zero. |
| P2.4 | T5 | Shared, then sweep | A `platform-paths` input on `gate.yml` (built on `changed-paths.yml`) decides per leg whether a PR runs Windows or macOS. The main tier always runs every leg. Sweep wordcraft Printing, effectcraft macOS, anicraft, craft-ai-integrations and craft-libs Workspace onto it. | macOS minutes on PRs drop by at least 80% in P0.1. |
| P2.5 | T6 | Per repository settings | Turn on the merge queue in repositories with more than 100 merges a week (photocraft, vectorcraft and others, per P0.1), with the main tier as its required check. | Merge-queue runs appear in P0.1. Main breakages caught before merge are logged. |

## Phase 3: remove duplicate work and make tests cheaper (weeks 3–6)

| Task | Requirement | Kind | Change | Done when |
|---|---|---|---|---|
| P3.1 | D1 | Shared, then photocraft | `gate.yml` input `gpu-group`. The main run excludes that nextest group (set through `NEXTEST_PROFILE` or `CRAFT_TEST_FILTER`). A following step runs it once on lavapipe with the silent-skip check, moved from photocraft's `scripts/ci-gpu-lavapipe.py`. Then photocraft drops its command chain. | The photocraft gate shows the GPU group once. Its time drops by about 5–7 min. |
| P3.2 | B1 | Shared, then pilot | Turn `sccache` on by default for Linux legs (read-only on PRs). Measure on the pilots. Windows follows once an sccache release includes mozilla/sccache#3026: bump the pin from v0.18.0, and re-enable it on photocraft's Windows legs. | Pilot test-build time drops in P0.1. Windows tracked as a follow-up. |
| P3.3 | B3 | Sweep, from data | Mark nightly-tier tests with `#[ignore = "nightly: <reason>"]`. Start with the RC6 patterns in the five heaviest apps, from P0.2's list (for example vectorcraft's `every_command_with_junk_params`, the wordcraft and vectorcraft "hostile/junk params" tests, linear-time guards, and the every-size variants beyond one size). Add the nightly job: `gate.yml` with `--run-ignored only` on a schedule. | Each pilot's PR-tier test time drops by at least 25%. The nightly job is green. |
| P3.4 | B4 | Shared, craft-contrib | The PR-tier nextest profile warns at 10 s. The house-rules check fails a new test over 30 s without a nightly mark. The house rules explain the tiers, so agents write tests that fit them. | A sample slow test is rejected. |
| P3.5 | B2 | Per app, from data | Where an app has many integration-test binaries (photocraft has 106 test binaries), merge them into fewer binaries per crate. Confirm debug info is at `line-tables-only` for tests. | Link time measured before and after on photocraft. |

## Phase 4: hold the line (weeks 4–8)

| Task | Requirement | Kind | Change | Done when |
|---|---|---|---|---|
| P4.1 | G1 | Shared, then per app | Move photocraft's 15 local jobs (Windows legs, corpus, web build, fuzz and perf jobs) into shared workflows or inputs, starting with the patterns more than one app needs. Delete jobs that no longer earn their minutes. Other apps' 0–2 local jobs follow. | Each app's workflows only call shared workflows, apart from the allowlist. |
| P4.2 | G2 | Shared | `ci-contract.yml`, run like `house-rules.yml`. It fails an app with a local job not on the allowlist, no `ready_for_review`, no PR `concurrency` cancel, or a macOS PR leg without a path filter. Swept into every app. | Every app passes. |

## Phase 5: the org's own runners (when available)

| Task | Requirement | Kind | Change | Done when |
|---|---|---|---|---|
| P5.1 | R1 | Shared | Every shared workflow takes its runner labels as an input (many already do through `os`). | One pilot app's gate runs on an org runner by changing inputs only. |
| P5.2 | R2 | Shared, org settings | Fork PRs never get org runners: label routing on `github.event.pull_request.head.repo.fork`, and org runner groups restricted to trusted workflows. | A fork PR is shown to stay on hosted runners. |
| P5.3 | R3 | Shared | On org runners, skip rust-cache and use a persistent `target/` per repository and branch tier, plus a local sccache. | Gate times on org runners recorded in P0.1. |

## Order and dependencies

- **Phase 0 comes first.** Without P0.1 nobody can tell whether a change helped, and without P0.3 a
  sweep across about 40 repositories is manual work.
- **P1.4 waits for P1.3** to be in every app.
- **P2.2 waits for P2.1.** P2.5 is better after P2.2, because the merge queue is what makes a lighter
  PR tier safe.
- **P3.1 and P3.3 are independent** and can run alongside phase 2.
- **P4 can start any time.** Each job moved into common-actions makes later sweeps cheaper.

## Rollback

Every shared change is an input or a condition:

- **Inputs** (`test-affected`, `gpu-group`, `sccache`, `platform-paths`): set back to the default.
- **Draft skip:** remove the condition.
- **Test tiers:** remove the `#[ignore]` marks, or label a PR `full-ci`.
- **Merge queue:** a repository setting.

## Reviews

- **End of each phase:** compare P0.1's weekly report with the baseline and the spec's estimates.
  Record the result here, and revise the estimates.
- **Before starting:** grill this plan and the spec with
  `/mattpocock-skills:grill-with-docs ci-optimisation`. The decisions most worth challenging are:
  - the three tiers, and whether main or the merge queue is the safety net;
  - SonarCloud option A or B;
  - the 30 s test budget;
  - which app jobs go in the contract allowlist.

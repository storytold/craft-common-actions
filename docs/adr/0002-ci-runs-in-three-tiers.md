# 2. CI runs in three tiers

- Status: proposed
- Date: 2026-11-15
- Spec: [docs/specs/ci-optimisation.md](../specs/ci-optimisation.md) (REQ-T1–T7, REQ-B3)

## Context

Every push to a pull request runs the full suite on every configured platform:

- the full gate;
- SonarCloud, which runs the suite a second time with coverage;
- photocraft's GPU group a second time on lavapipe;
- macOS and Windows legs.

PRs are high-volume and mostly opened by agents. wordcraft ran about 13 PR gate runs for each merge.
In the week of 8–14 Nov, 82% of the org's runs came from pull requests, and the suite used at least
1.40M Linux-equivalent minutes. Most PRs touch one or two crates, so most of that work re-proves code
the PR didn't change.

## Decision

Every check belongs to exactly one tier.

| Tier | Runs on | Contents |
|---|---|---|
| PR | `pull_request`, drafts included | fmt, clippy, the affected tests on one Linux leg (a nextest filter of the changed packages and their dependents), cheap policy checks, and platform legs only when platform paths changed |
| Main | Every push to main, and the merge queue once enabled | The full suite, every platform leg, SonarCloud, the GPU group on lavapipe |
| Nightly | Schedule | Tests marked `#[ignore = "nightly: <reason>"]` (fuzz-style, timing guards, every-size and exhaustive sweeps), fuzzing, FreeBSD, perf |

- The `full-ci` label promotes a PR to the main tier.
- A change that can affect everything (lockfile, root manifest, toolchain, Cargo or nextest
  configuration, xtask, workflows, files outside a package) runs the full suite on the PR tier too.

## Consequences

- PRs get feedback in about 20 minutes instead of 45, and use a fraction of the minutes.
- Main, or the merge queue, becomes where cross-crate and cross-platform breaks get caught. Until
  the merge queue is on, some breaks will land on main and be fixed forward. The merge queue
  (REQ-T6) turns that into a pre-merge check.
- A test marked nightly can hide a regression for up to a day. The weekly report shows nightly
  failures, and each mark needs a reason.
- Each app's `xtask` must honour `CRAFT_TEST_FILTER`. Apps must list tests that read another crate's
  files, so changes to those files force the full suite.

## Alternatives considered

- **Keep the full suite on every PR.** Rejected: that's the cost this decision removes.
- **Select packages with `cargo -p`, to also cut the build.** Rejected for now: a subset changes
  feature unification, which rebuilds dependencies and misses the cache. The nextest filter keeps
  today's build and cuts run time. Build time is addressed by sccache (REQ-B1).
- **Full suite only in a merge queue, nothing on PRs.** Rejected: a PR needs a fast signal before
  review. The merge queue is part of this decision, not a replacement for the PR tier.
- **Faster runners.** Larger hosted runners are billed even on public repositories, and the org's own
  runners are coming. Faster runners shorten the wait but don't remove the duplicated work.

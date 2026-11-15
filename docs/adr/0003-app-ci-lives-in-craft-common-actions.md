# 3. App CI lives in craft-common-actions

- Status: proposed
- Date: 2026-11-15
- Spec: [docs/specs/ci-optimisation.md](../specs/ci-optimisation.md) (REQ-G1–G3)

## Context

The craft apps call shared workflows from this repository (`gate.yml`, `sonarcloud.yml`,
`freebsd.yml`, `release.yml` and others). Each app still owns its own `.github/workflows/*.yml`,
`xtask` and `.config/nextest.toml`.

- Most apps define only 0–2 jobs of their own. photocraft defines 15: Windows legs, corpus, web
  build, fuzz and perf jobs, and a GPU command chain inside its gate call.
- `gate.yml`'s header already states the intended rule: app-specific checks belong here, as shared
  workflows or inputs, and an app's workflows only call this repository's workflows. Nothing
  enforces it.
- The apps have no owners. A fix that has to be made app by app either doesn't happen or drifts.
  Two recent examples:
  - photocraft's Windows rust-cache broke silently on 13 Nov;
  - the duplicate GPU run could only be fixed inside photocraft.

## Decision

1. **App workflows only call shared workflows.** App-specific behaviour becomes an input with a
   default that changes nothing for other callers.
2. **App-local jobs move here or are deleted.** A short allowlist covers jobs that genuinely belong
   to one app and that no other app will need. Each entry carries a reason.
3. **A shared `ci-contract.yml` check,** run like `house-rules.yml`, enforces the rule and the
   suite's CI conventions in every app:
   - the allowlist;
   - `ready_for_review` on PR triggers;
   - PR concurrency cancellation;
   - path filters on PR macOS legs.
4. **Changes that must touch every app go through the sweep tool** (`scripts/sweep.py`). It opens one
   PR per app, and the central team merges them.

## Consequences

- One change here reaches every app. The contract check stops apps drifting back without needing an
  owner to watch.
- Shared workflows gain inputs. Each must default to today's behaviour and have tests, or the shared
  workflows become a tangle of special cases.
- photocraft's migration is the largest piece of work. Its jobs are also the most expensive, so it
  pays back first.
- An app can no longer add a CI job alone. It adds an input here, or an allowlist entry, through
  review in this repository.

## Alternatives considered

- **Leave CI in each app and document conventions.** Rejected: with no owners, conventions are
  nobody's job to follow.
- **Generate each app's workflows from a template.** Rejected: generated files drift as soon as
  someone edits them by hand, and reusable workflows already give one source of truth.
- **One monorepo for CI configuration, checked out by every app.** Rejected: reusable workflows at
  `@main` already do this with less machinery.

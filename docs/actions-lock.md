# Keeping `.github/workflows/actions.lock` current

Every Storytold repository with workflows carries `.github/workflows/actions.lock`. It records the
commit behind each action a workflow runs, so an action's tag moving cannot change what a run
executes. The `Lockfile` workflow ([`lockfile.yml`](../.github/workflows/lockfile.yml)) fails when
the lockfile and the workflows disagree, which means the lockfile has to be regenerated whenever a
workflow changes.

## After editing a workflow

Run the command CI runs, from the repository root:

```sh
gh extension install github/gh-actions-lock --pin v0.4.1   # once
gh actions-lock
```

Commit `.github/workflows/actions.lock` with the workflow change. `gh actions-lock --verify`
reproduces the CI check locally and exits non-zero when the lockfile is stale.

Two rules for pins:

- Write action pins as tags (`actions/checkout@v7.0.1`), not commits. The lockfile holds the commit;
  a `@<sha>` pin has to be rewritten by hand every time the action moves.
- Keep the trailing `# vX.Y.Z` comment accurate. It is the tag the lockfile records, and the one
  Dependabot rewrites.

## Why Dependabot updates it

Each repository's `.github/dependabot.yml` lists `github-actions`. When Dependabot opens a version
update for an action in an onboarded workflow, it regenerates the matching `actions.lock` entry in
the same pull request. Nothing else is needed, and no bot touches the lockfile on its own.

Renovate does not read `actions.lock`, so it would leave the lockfile stale after an action bump.
That is why action updates go through Dependabot.

Dependabot only maintains workflows that are already onboarded: a newly added workflow needs one
`gh actions-lock` run before its actions are managed, and the `Lockfile` check will ask for it.

## What the check covers

`lockfile.yml` runs on every pull request, on pushes to the default branch, and on demand. It
installs the extension at a pinned version and runs `gh actions-lock --verify`, which fails both on
drift between workflows and lockfile, and on a locked pin whose tag no longer resolves to the
recorded commit.

## Sonar

SonarCloud's `githubactions:S7637` flags the commit pins the lockfile contains. Repositories with a
lockfile ignore that rule for `.github/workflows/**` in `sonar-project.properties`; the comment
there explains why. The finding is a false positive, not something to fix by unpinning actions.

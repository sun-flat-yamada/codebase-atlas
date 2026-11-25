# Changelog

All notable changes to codebase-atlas are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).
Changes inherited from reverse-spec-flow are recorded in [docs/release-notes.md](docs/release-notes.md).

## [Unreleased]

### Fixed

- `automerge.py finish` no longer treats "no check run yet" as passed, updates a branch that is behind `main`
  (required by the strict branch protection) before waiting for CI again, and stops on requested changes or
  unresolved review threads, as `docs/development-workflow.md` already stated. The whole flow is now unit-tested.
- `atlas_graph.py build` reads `SOURCE_PATH` / `SOURCE_EXCLUDE` from `gate-config.env` and writes to
  `01-code-graph/graphify` by default, so the graph is built with the same exclusions as `G-GRAPHIFY`; it converts
  Git Bash paths (`/c/...`), keeps `GRAPHIFY_CMD` paths with spaces intact, and also detects changes to a target
  that is not a git repository.
- graphify is pinned to the tested commit (`GRAPHIFY_TESTED_REF`) in CI and in the install instructions, and
  `atlas_graph.py` warns about an unknown `graph.json` schema version.
- `automerge.py finish` reports a head branch that GitHub already deleted on merge ("Automatically delete head
  branches") as deleted instead of asking for a manual deletion.
- Generated repositories record `framework=codebase-atlas` (and `based_on=reverse-spec-flow 2.2.1`) in
  `FRAMEWORK-VERSION`.

## [0.1.0] - 2026-11-25

First release, based on reverse-spec-flow 2.2.1.

### Added

- **graphify as the code analysis tool** (GP-108).
  - `tools/atlas_graph.py` (Python standard library only): `build` runs graphify (`extract --code-only` and
    `cluster-only`) outside the target, records the source commit in `build-info.json` and writes an `inventory.md`
    (files per directory, cross-directory dependencies, highly connected symbols); `crosscheck`, `edge-gap`,
    `inventory` and `stale` compare the graph with `code-graph.md`.
  - `init.sh --code-graph-tool graphify|none` (default `graphify`, overridable with `ATLAS_CODE_GRAPH_TOOL`), which
    writes `CODE_GRAPH_TOOL` to `gate-config.env` and copies the helper to `spec-extraction/tools/`.
  - Gates `G-GRAPHIFY` (FAIL when a source file found by graphify belongs to no node of `code-graph.md`, or when the
    graph was built at a different commit than `scope.md`) and `G-GRAPHIFY-EDGE` (WARN for module dependencies missing
    from the edge registry), run by `gates.sh phase1` and `gates.sh graphify`.
  - Step 1-1b in the procedure, GP-108 in `pipeline.md`, selftest T7, and `docs/graphify-integration.md`.
  - Paths that point at the whole target (`.`, `*`, `**/*`) do not count as coverage (`BROAD`), so the gate cannot be
    passed with one catch-all node. `G-GRAPHIFY` runs again in `gates.sh phase4` before completion is declared.
  - `build` records the files graphify did not parse, by extension (`not_in_graph_by_ext` in `build-info.json` and a
    section in `inventory.md`), so languages graphify does not support stay visible for the Playbook's enumeration.
- **Auto-Pilot merge**, ported from github-copilot-dashboard: `scripts/automerge.py` (`mode`, `finish`, `branch`),
  `CHG_DEV_AUTO_PILOT=true` in `.env.example`, the `Branch Name Check` workflow, and
  `docs/development-workflow.md`. Pull requests from forks are never auto-merged (and their head branch is never deleted).
- GitHub repository setup: CI (Python unit tests on 3.10/3.12/3.13, ruff, shell syntax, selftest, graphify
  end-to-end), secret scanning (gitleaks), Dependabot for GitHub Actions, issue and pull request templates,
  CODEOWNERS, FUNDING (Buy Me A Coffee), SECURITY, SUPPORT, NOTICE and a Contributor Covenant code of conduct.

[0.1.0]: https://github.com/sun-flat-yamada/codebase-atlas/releases/tag/v0.1.0

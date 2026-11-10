# ADR-0016: Homegrown test runner instead of pytest

| Field | Value |
|---|---|
| Status | Accepted (owner approved 2026-11-10; decision as recorded in `CLAUDE.md`, "No pytest"). |
| Date | 2026-07-12 (decision recorded in the migration plan that was not executed) |
| Deciders | Martin Werner |
| Affects | `src/mcapp/*_tests.py`, `src/mcapp/storage/*_tests.py`, `scripts/*_tests.py`, `scripts/run_startup_tests.py`, `scripts/gate.sh` |
| Supersedes | - |
| Superseded by | - |

## Context

The project has several hundred tests in `*_tests.py` modules. Each has a `main()`-style entry, and
`scripts/run_startup_tests.py` runs the suites in isolated state. A plan on 2026-07-12 proposed
migrating all of them to pytest (status READY, all decisions locked, Wave 0 not started).

## Decision

Keep the homegrown runner. `scripts/run_startup_tests.py` is the canonical, exit-code-gated runner.
Suites are registered in its `main()`. A suite not registered there is not gated. The in-app startup
path runs only a non-fatal smoke check, and the command suite never runs inside the live service
because it would mutate live state.

## Consequences

- No test framework dependency, and the same runner gates CI-less releases through `scripts/gate.sh`.
- Adding a suite means registering it in `main()`. A reviewer must check that, because nothing else does.
- The test code is not idiomatic pytest. Migrating later means a new ADR that supersedes this one.

## Alternatives considered

- pytest migration (plan of 2026-07-12): not executed. It would have rewritten several hundred tests and
  changed the isolation model that the runner relies on.

## Evidence

- `CLAUDE.md`, section "Testing"
- `../archive/implementation-pytest.md` (the plan)
- `pyproject.toml`: `PT` (pytest style) is only a ruff rule, no pytest dependency

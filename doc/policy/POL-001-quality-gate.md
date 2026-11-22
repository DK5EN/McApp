# POL-001: Quality gate and tooling

| Field | Value |
|---|---|
| ID | POL-001 |
| Status | REVIEWED (approved by owner 2026-11-10) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (approved 2026-11-10) |
| Next review due | 2027-05-10 |
| Applies to | all code and docs in this repo |
| Sources | `CLAUDE.md` ("Development Commands", "Code Quality", "Type Checking", "Testing"); `scripts/gate.sh` (header and step list); `pyproject.toml` (`[tool.ruff*]`, `[tool.mypy]`) |

## 1. Rule

1. **Python is run with `uv` only.** Never `pip`, never `venv`, never `uvx mypy` or `pipx run mypy`.
   Use `uv run mypy` in the project environment, after `uv sync --all-packages`.
2. **Before a commit:** `uvx ruff check`, `uvx ruff format --check .` and `uv run mypy src/mcapp ble_service/src`
   are clean. `ruff format` also formats `python` code blocks in Markdown, so a docs-only commit must pass it.
3. **Before a release:** `./scripts/gate.sh` ends with `GATE: PASS`. Read its exit code directly, never through `| tail`.
4. **Types are strict.** Both source roots stay at zero mypy errors. `# type: ignore` is the last resort, always
   with a code and a reason.
5. **Tests use the homegrown runner.** A suite is only gated if `scripts/run_startup_tests.py` registers it
   (ADR-0016).
6. **Ruff config is identical** in `pyproject.toml`, `ble_service/pyproject.toml` and mc-chat's
   `pyproject.toml`, so the classifier subtree lints the same everywhere.
7. **New `# noqa` markers** need a trailing reason and should be rare. A real fix comes first.

## 2. Why

The gate is the only check that runs on every release. CI's `tests` workflow is disabled, so a
failing local check is a failing release. Strict types and the homegrown runner exist because the
production box cannot be debugged interactively: a silent regression reaches an operator's radio.

## 3. Verify

```bash
./scripts/gate.sh mcproxy        # this repo only, one exit code
echo $?                          # 0 = PASS
```

## 4. Exceptions

- A docs-only change still runs the ruff format check (see rule 2).
- The webapp checks belong to the webapp repo; `gate.sh` runs them when the sibling is present.

## 5. Related

- Steps for a release: `../sop/SOP-001-release.md`
- Test suites and their registration: `../reference/architecture-reference.md` (section "Testing")

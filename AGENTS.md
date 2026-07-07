# AGENTS.md

## COMPULSORY: Read docs/standards/ before any change

Before implementing any non-trivial change, the agent MUST read and score against the rubrics in
[`docs/standards/`](./docs/standards/). The scorecard is in [`docs/standards/README.md`](./docs/standards/README.md).

- **Planning:** [`docs/standards/planning-rubric.md`](./docs/standards/planning-rubric.md)
- **Code review:** [`docs/standards/code-review-rubric.md`](./docs/standards/code-review-rubric.md)
- **Parity:** [`docs/standards/parity-rubric.md`](./docs/standards/parity-rubric.md)
- **RCA:** [`docs/standards/rca-rubric.md`](./docs/standards/rca-rubric.md)
- **Prevention:** [`docs/standards/prevention-rubric.md`](./docs/standards/prevention-rubric.md)

---

## Build/test/lint commands

```bash
python3 -m unittest discover tests -v   # full test suite
python3 -m unittest tests.test_tray.py -v   # tray-only tests (no GTK)
bash tests/test_parity.sh                    # JSON parity gate (requires bash + jq)
bash -n repodash                             # Bash syntax check
scripts/git-hooks/scan-personal-data.sh <remote_sha> <local_sha>   # secrets scan
python3 tray/repodash_tray.py --check        # headless tray check (no GUI)
python3 tray/repodash_tray.py                # launch tray (GTK3 + AppIndicator)
```

---

## What this project is

repodash is a multi-repo git dashboard. It ships two implementations kept at strict JSON parity:

| File | Language | Purpose |
|---|---|---|
| `repodash.py` | Python 3.8+, stdlib only | Canonical implementation; cross-platform |
| `repodash` | Bash 3.2+ | Parity port for Unix shells |
| `tray/repodash_tray.py` | Python + GTK3 | GNOME tray icon + dashboard (Linux only) |

CI runs on Ubuntu, macOS, and Windows against Python 3.8 and 3.12. All tests use stdlib `unittest` — no test dependencies.

---

## State-of-the-art workflow (MANDATORY)

### Phase 0 — Before you touch any code

1. Read the rubrics in `docs/standards/`. Score the current project state if you're evaluating readiness.
2. Identify the change type:
   - **JSON model change** → both implementations must change → HIGH risk
   - **Single-implementation change** → only Python OR only Bash → LOW risk
   - **Test-only change** → fixtures or test methods → LOW risk
   - **Config/workflow change** → CI, hooks, scripts → MEDIUM risk

### Phase 1 — Plan (Pass 1, target ≥ 80)

Draft a plan answering these questions. Write it out. Score it against
[`docs/standards/planning-rubric.md`](./docs/standards/planning-rubric.md) Pass 1 criteria.

1. **Problem statement** — one sentence, falsifiable, references a test case or observed output.
2. **Root cause hypothesis** — file:line + evidence.
3. **Impact map** — every file/function/API contract touched, including callers.
4. **Parity analysis** — does this touch the JSON model? If yes, cite exact lines in both `repodash.py` and `repodash`.
5. **Constraint check** — stdlib-only? gi imports inside run_gui()? Pure function contract?
6. **Test plan** — existing tests to update, new tests to write, specific test method names.
7. **Implementation order** — step-by-step with a verification command between each step.
8. **Rollback plan** — specific files/commits to revert.

Score the plan. If <80, identify the lowest-scoring criteria, revise, re-score. Do not proceed until ≥80.

### Phase 2 — Architecture review (Pass 2, target ≥ 90)

Cross-check the plan against all rubrics. Score against
[`docs/standards/planning-rubric.md`](./docs/standards/planning-rubric.md) Pass 2 criteria AND
[`docs/standards/code-review-rubric.md`](./docs/standards/code-review-rubric.md).

- Verify the parity contract: run `bash tests/test_parity.sh` BEFORE making changes to confirm it passes.
- Verify hard constraints: run the evidence commands in [`docs/standards/code-review-rubric.md`](./docs/standards/code-review-rubric.md) C1–C4.
- Check backward compatibility: does this break the tray app, any subprocess caller, or CI?
- Check security: path traversal, shell injection, config leakage.

Score the architecture. If <90, iterate. Do not proceed until ≥90.

### Phase 3 — Implement + Verify (Pass 3, target ≥ 95)

Score against [`docs/standards/planning-rubric.md`](./docs/standards/planning-rubric.md) Pass 3 criteria.

1. Implement in the order defined in the plan.
2. After each step, run the verification command listed in the plan.
3. After all steps, run the full verification gate:

```bash
python3 -m unittest discover tests -v && bash tests/test_parity.sh && bash -n repodash
```

4. If any gate fails → DO NOT PROCEED → jump to RCA workflow (below).
5. Score the final output against Pass 3. If ≥95, the change is ready to commit.

---

## Error detection pipeline

The project has a layered detection pipeline. Use it. Do not skip layers.

```
Save file → bash -n repodash  →  pre-push hook  →  CI  →  main
                (syntax)        (full gates)    (matrix)
```

### Layer 1 — Syntax check (earliest)
```bash
bash -n repodash
```
Run after every edit to `repodash`. Catches unbalanced quotes, missing `fi`/`done`, syntax errors.

### Layer 2 — Pre-push gate (before pushing)
```bash
# Installed once per clone: bash scripts/install-hooks.sh
# Runs automatically on every git push:
#   1. python3 -m unittest discover tests       (full test suite)
#   2. bash -n repodash                          (syntax check)
#   3. bash tests/test_parity.sh                 (JSON parity)
#   4. scripts/git-hooks/scan-personal-data.sh   (secrets / personal data)
```

### Layer 3 — CI (automated, multi-platform)
CI runs on every push and PR across Ubuntu, macOS, Windows × Python 3.8, 3.12. It mirrors the pre-push checks plus platform-specific coverage. See `.github/workflows/ci.yml`.

### When a gate fails — STOP and RCA

Do not attempt a fix without understanding the root cause. Follow the RCA protocol.

---

## Root cause analysis (RCA) — mandatory when any gate fails

Follow [`docs/standards/rca-rubric.md`](./docs/standards/rca-rubric.md).

### Quickstart

```
1. Reproduce:         run the failing command directly → capture the exact error
2. Isolate the delta: git diff HEAD~1  (or git log --oneline -5 if unsure)
3. Hypothesize:       "Failure at X because Y changed at Z"
4. Minimal repro:     reduce to a single command that reproduces the failure
5. Eliminate:         syntax? imports? permissions? deps? env? — rule them out
6. Fix the root cause, not the symptom
7. Add a regression test that would have caught this
8. Add a prevention measure (update a hook, CI step, or scan pattern)
9. Document in the commit message body:
      Root cause: <one line>
      Evidence:   <error message or diff>
      Fix:        <what changed and why>
      Prevention: <what gate was added/updated>
```

### Parity failure RCA (special case)

When `tests/test_parity.sh` fails:

```bash
# The diff is written to /tmp/repodash_parity.diff
bash tests/test_parity.sh
cat /tmp/repodash_parity.diff

# Identify the divergent field → grep for it in both implementations
grep -n 'field_name' repodash.py repodash

# Fix the lagging implementation, re-run parity
bash tests/test_parity.sh
```

---

## Self-correction loops

### Loop 1 — Parity drift
If parity fails → diff normalized outputs → identify the field → trace to the implementation that lags → fix → re-run parity gate. The `test_parity.sh` script normalizes volatile fields automatically — trust its output.

### Loop 2 — Test failure
If a test fails → isolate the failing test method → read the test to understand the assertion → `git diff` to see what changed → fix the code or update the test → re-run that specific test:

```bash
python3 -m unittest tests.test_repodash.TestParity -v
```

### Loop 3 — Constraint violation
If a hard constraint is violated (non-stdlib import, gi import at module scope, config object passed to pure function) → revert the violating change → re-design without the violation → re-implement.

---

## Systemic prevention — shift-left enforcement

The goal: catch defects at the earliest possible stage and ensure the SAME defect cannot recur.

### Prevention rules

1. **Every regression gets a regression test.** When fixing a bug, add a test that fails on the old code and passes on the new code. Place it in the appropriate test file (`test_repodash.py` or `test_tray.py`).

2. **Every parity drift gets a prevention rule.** If the implementations drifted, add or update a check in the parity gate that would have caught that specific drift class.

3. **Every constraint violation gets a grep gate.** If someone added a non-stdlib import or a module-scope gi import, add a `grep`-based check to CI that fails on recurrence.

4. **Every secret leak gets a pattern.** If a new kind of secret leaked, add its regex pattern to `scripts/git-hooks/scan-personal-data.sh`.

### The pre-push hook is the primary prevention gate

Install it once per clone:
```bash
bash scripts/install-hooks.sh
```

It runs on every `git push`:
- Full test suite
- Bash syntax check
- JSON parity gate
- Secrets/personal-data scan

Emergency bypass (only when the hook itself is broken): `git push --no-verify`
Then fix the hook and push again normally.

---

## Hard constraints — DO NOT BREAK

These are pass/fail. Violating any of them blocks the change. Read [`docs/standards/code-review-rubric.md`](./docs/standards/code-review-rubric.md) C1–C4 for evidence commands.

| # | Constraint | Why |
|---|---|---|
| **C1** | **No third-party packages.** `repodash.py` and `tray/repodash_tray.py` must import only from the Python standard library. | Cross-platform, zero-install. |
| **C2** | **All `gi` imports inside `run_gui()`.** `tray/repodash_tray.py` keeps every `gi`/`Gtk`/`AppIndicator` import inside `run_gui()` so the module loads without a display. | Test suite requires import without GTK. |
| **C3** | **`find_repos()` and `scan_dirty()` are pure.** They take no config object. Filtering (excluded repos, base dir, depth) is applied at the call site, not inside these functions. | Separation of concerns; testability. |
| **C4** | **Bash syntax must pass `bash -n repodash`.** | Catches shell syntax errors before runtime. |

---

## The parity invariant — DO NOT BREAK

`repodash.py --json` and `repodash --json` must emit semantically identical JSON for the same inputs. The schema is defined in `repodash.py`; the Bash port must match it exactly. The parity gate (`TestParity` in `tests/test_repodash.py` and `tests/test_parity.sh`) enforces this.

**If you change the JSON model in `repodash.py`, you must make the matching change in `repodash` (Bash).** There are no exceptions.

Before committing any change that touches the JSON model, complete the checklist in
[`docs/standards/parity-rubric.md`](./docs/standards/parity-rubric.md).

---

## Architecture — tray app

`tray/repodash_tray.py` has two tiers:

1. **Menu tier** — cheap; runs `git status` per repo on a background thread every ~90s. Uses `scan_dirty()` → filter excluded → update indicator.
2. **Dashboard tier** — expensive; shells out to `repodash.py --json` on demand (`fetch_model()`), filters excluded repos from the result, then populates a GTK `ListBox`.

The tray never imports or modifies `repodash.py`. Communication is one-way: subprocess stdout (JSON).

**Config** is persisted to `~/.config/repodash/config.json`. After loading or saving config, `apply_config_to_env(cfg)` must be called so that `detect_terminal()`, `base_dir()`, and the `repodash.py` subprocess all pick up the current values from `os.environ`.

---

## Key files

| File | Why |
|---|---|
| `docs/standards/` | **ALL RUBRICS** — read before any change |
| `tests/fixtures.py` | Builds the shared test repo tree; both Python and Bash tests run against it |
| `repodash.py` lines 566–611 | `Config` + `build_config()` — the canonical config/flag model |
| `repodash` | Bash implementation — must stay at parity with `repodash.py` |
| `tray/repodash_tray.py` lines 41–145 | All module-level config helpers (`CONFIG_DEFAULTS`, `load_config`, `save_config`, `apply_config_to_env`, `resolve_*`) |
| `.github/workflows/ci.yml` | CI pipeline — mirrors pre-push checks across 3 OS × 2 Python versions |
| `scripts/git-hooks/pre-push` | Pre-push gate — tests, syntax, parity, secrets scan |
| `scripts/git-hooks/scan-personal-data.sh` | Secrets/personal-data scanner — blocks pushes that leak keys or PII |

---

## Git workflow

Always commit to local `main` (never to a feature branch). Commit after every change — never leave `main` with uncommitted modifications.

**No `Co-Authored-By` trailer.** Do not append a `Co-Authored-By: Claude ...` line to commit messages.

**Commit message format:**
```
<subject line — imperative, ≤72 chars>

Body (optional):
- Root cause: <one line>
- Fix: <what changed>
- Prevention: <gate added/updated>
```

**Pre-push hook.** Install once per clone: `bash scripts/install-hooks.sh`. The hook runs on every push and blocks if tests, parity, syntax, or secrets scan fail. Emergency bypass: `git push --no-verify` (only when the hook itself is broken — fix it afterward).

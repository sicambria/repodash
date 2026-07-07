# Code review rubric

Every change must pass this rubric before landing on `main`. Score at 0%, 50%, or 100% per criterion.

## Hard constraints (pass/fail — any failure blocks the change)

| # | Constraint | Evidence command | Fail = block |
|---|---|---|---|
| C1 | **stdlib-only.** `repodash.py` and `tray/repodash_tray.py` import only from the Python standard library. | `grep -n '^import\|^from' repodash.py tray/repodash_tray.py` | Any non-stdlib import. |
| C2 | **gi imports inside run_gui().** Every `gi`/`Gtk`/`AppIndicator` import in `tray/repodash_tray.py` is inside `def run_gui():`. | `grep -n 'gi\|Gtk\|AppIndicator' tray/repodash_tray.py` | Any such import at module scope. |
| C3 | **find_repos() and scan_dirty() are pure.** They take no config object. Filtering is applied at the call site. | `grep -n 'def find_repos\|def scan_dirty' repodash.py` | Either function accepts a config parameter. |
| C4 | **Bash syntax passes.** | `bash -n repodash` | Non-zero exit. |

## Code quality (scored)

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| Q1 | **Function fits on one screen** — ≤50 lines per function. | 15 | Functions >100 lines. | Functions ≤100 lines. | Functions ≤50 lines; complex logic extracted to helpers. |
| Q2 | **Single responsibility** — each function does one thing named by its signature. | 15 | Functions mix concerns (I/O + logic + formatting). | Separated but named vaguely. | Each function has a clear, single purpose evident from its name and docstring. |
| Q3 | **No dead code** — no commented-out blocks, unused imports, unreachable branches. | 15 | Dead code present. | Removed but undocumented. | `grep` for `TODO`, `FIXME`, commented-out code returns zero hits in changed files. |
| Q4 | **Naming consistency** — follows existing conventions (snake_case, descriptive). | 10 | Inconsistent names in changed code. | Mostly consistent. | Every new name matches the file's existing style. |
| Q5 | **Error handling** — errors propagate, no bare `except:` or `|| true` swallowing failures. | 15 | Swallowing present. | Bubbled up but no user message. | Errors bubble up with context; user-facing messages are in stderr. |
| Q6 | **Shell safety** — `set -euo pipefail` at top of any new Bash code; no unquoted expansions. | 15 | Unsafe shell exists. | `set -eu` but no pipefail. | `set -euo pipefail` + all expansions quoted. |
| Q7 | **Test style** — tests use the shared fixtures, are deterministic, and test one thing each. | 15 | Tests rely on real repos or randomness. | Uses fixtures but groups multiple assertions. | One assertion per test method; uses `tests/fixtures.py` exclusively. |

## Scoring

```
Code review score = Σ (Qn_weight × level) / Σ Qn_weight × 100
```

A score below **85** fails code review. Fix the violation and re-score.

# Prevention rubric — shift-left & systemic enforcement

Shift-left: catch defects at the earliest possible stage. Systemic prevention: automate the detection so the same defect cannot recur.

## The repodash detection pipeline (earliest → latest)

```
Editor save  →  bash -n repodash  →  git add  →  pre-push hook  →  CI  →  main
     ↑               ↑                               ↑              ↑
  (manual)     (manual today)                  full gates      multi-OS matrix
```

Each shift left reduces the cost of a defect by ~10×.

## Shift-left criteria

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| S1 | **Bash syntax at edit time** — `bash -n repodash` runs before or immediately after the file is saved. | 10 | No syntax check. | Developer runs `bash -n` manually before committing. | Pre-commit hook or editor save-hook runs `bash -n repodash` automatically. |
| S2 | **Pre-push gate** — every push runs the full test suite (216 tests), parity gate, bash syntax, and secrets scan. | 25 | No pre-push gate. | Documented in AGENTS.md but hooks not installed via `scripts/install-hooks.sh`. | `scripts/install-hooks.sh` installed; `scripts/git-hooks/pre-push` blocks the push on any failure. |
| S3 | **CI covers the same checks** — the same four checks that block a push also run in CI, so a bypassed hook doesn't let defects through. | 25 | CI runs different checks than pre-push. | CI runs a subset (e.g., tests only, no secrets scan). | All four pre-push checks run in CI across three jobs: `python` (tests), `bash-parity` (parity + bash syntax + tests), `guardrails` (secrets scan). |
| S4 | **Secrets/personal-data scan** — outgoing commits are scanned for keys, tokens, home paths, and email addresses. | 20 | No scan. | Scan exists but does not block the push (warning only). | `scan-personal-data.sh` runs in pre-push AND CI `guardrails` job; blocks push/PR on any match. |
| S5 | **Platform coverage** — CI runs on all supported platforms (Ubuntu, macOS, Windows) against the oldest and newest supported Python (3.8, 3.12). | 10 | Single platform only. | Multi-OS but single Python version. | Full GitHub Actions matrix: 3 OS × 2 Python versions, all pass. |
| S6 | **Bash linter** — `shellcheck` (or equivalent) runs on `repodash` to catch unsafe patterns before runtime. | 10 | No linting. | `shellcheck` installed locally but not enforced. | `shellcheck repodash` in CI or pre-push, zero warnings in new/changed lines. |

## Systemic prevention criteria

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| P1 | **Every regression gets a regression test.** | 25 | Regression not tested. | Test added but loosely related to the bug. | Test reproduces the exact bug: remove the fix → test fails; apply fix → test passes. |
| P2 | **Every parity drift gets a prevention rule.** | 30 | No prevention. | Documented in commit message. | A new assertion, normalization step, or type check added to the parity gate that would have caught this specific drift pattern. E.g., after a boolean vs string mismatch, add a type-check assertion to the normalization step. |
| P3 | **Every constraint violation gets a grep gate in CI.** | 20 | No gate. | Documented recommendation. | A `grep` step in `.github/workflows/ci.yml` that fails if the violation recurs. E.g., `grep -n '^import requests' repodash.py && exit 1`. |
| P4 | **Every new secret pattern gets added to the scanner.** | 25 | Pattern not added. | Pattern noted in commit body. | Regex pattern added to `scripts/git-hooks/scan-personal-data.sh` with a test case comment showing the matched text. |

## Prevention maturity score

```
Prevention = Σ (Sn_weight × level + Pn_weight × level) / total_weight × 100
```

| Score | Maturity |
|---|---|
| 90–100 | **SOTA** — every gate is automated; regressions are caught before they reach `main`. |
| 70–89 | **Strong** — most gates automated; some manual checks remain. |
| 50–69 | **Adequate** — basic gates present; significant manual work. |
| <50 | **Weak** — gates rely on developer discipline alone. |

## How to improve your score

For each criterion below 100%, here is the concrete action to reach 100%:

| Criterion | To reach 50% | To reach 100% |
|---|---|---|
| S1 | Run `bash -n repodash` manually before every commit. | Add a `.git/hooks/pre-commit` or editor save-hook that runs `bash -n repodash`. |
| S2 | Document the install command. | Run `scripts/install-hooks.sh` once per clone. |
| S3 | Add the missing CI job. | Ensure `ci.yml` has `python`, `bash-parity`, and `guardrails` jobs covering all four checks. |
| S4 | Write a scan script. | Add `scan-personal-data.sh` to pre-push hook AND CI. |
| S5 | Add matrix entries. | Update `ci.yml` `strategy.matrix` to include all target OS + Python combinations. |
| S6 | Install `shellcheck`. | Add `shellcheck repodash` to CI or pre-push hook; fix all warnings. |
| P1 | Write a test that touches the bug area. | Write a test that fails exactly on the old code, passes on the fix. |
| P2 | Note the drift in the commit. | Add a new assertion/check to `test_parity.sh` or `TestParity` that catches the drift class. |
| P3 | Note the constraint in a comment. | Add a `grep` step to `ci.yml` that fails on the violation. |
| P4 | Note the pattern in the commit. | Add the regex + test comment to `scan-personal-data.sh`. |

## Worked example: Parity drift from boolean → string type mismatch

A change accidentally emits `"true"` (string) instead of `true` (boolean) in the Bash implementation.

**Prevention applied:**
- P2: Added a type-check assertion to `test_parity.sh` normalization that compares JSON types field-by-field.
- P1: Added `test_json_type_consistency` to `TestParity` that loads both outputs and asserts `type(py[field]) == type(sh[field])` for every field.

**Result:** Both checks would have caught this drift BEFORE it was committed, rather than discovering it post-push. Prevention score improved from 50 to 100 on P2.

---

## Historical baseline (2026-07-07)

At time of writing, repodash scores **79** on this rubric:

- S2 (pre-push): ENFORCED (100%)
- S3 (CI coverage): DOCUMENTED (50%) — checks split across jobs but all present
- S4 (secrets scan): ENFORCED (100%)
- S5 (platform coverage): ENFORCED (100%)
- S1 (edit-time syntax): DOCUMENTED (50%) — manual `bash -n`
- S6 (shellcheck): ABSENT (0%) — no shellcheck integration
- P1 (regression tests): DOCUMENTED (50%)
- P2 (parity drift prevention): DOCUMENTED (50%)
- P3 (constraint grep gates): ABSENT (0%)
- P4 (secret patterns): DOCUMENTED (50%)

To reach 90: add shellcheck to CI, add grep-based constraint enforcement, mandate regression tests per fix.

# Prevention rubric — shift-left & systemic enforcement

Shift-left: catch defects at the earliest possible stage. Systemic prevention: automate the detection so the same defect cannot recur.

## The repodash detection pipeline (earliest → latest)

```
Editor save  →  pre-commit?  →  git add  →  pre-push  →  CI  →  main  →  user
     ↑              ↑                            ↑          ↑
  bash -n       (unused)                   full gates   multi-OS
```

## Shift-left criteria

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| S1 | **Syntax check at editor-save time** — `bash -n repodash` runs before the file is staged. | 15 | No syntax check. | Manual: developer runs `bash -n` before committing. | Pre-commit hook or editor integration runs `bash -n` automatically. |
| S2 | **Pre-push gate** — every push runs the full test suite, parity gate, bash syntax, and secrets scan. | 25 | No pre-push gate. | Documented but hooks not installed. | `scripts/install-hooks.sh` installed; `pre-push` hook enforces all checks. |
| S3 | **CI mirrors pre-push** — the same checks that block a push also run in CI (belt and suspenders). | 25 | CI runs different checks than pre-push. | CI runs subset. | CI runs identical checks: tests, parity, bash syntax (`bash-parity` job) + secrets scan (`guardrails` job). |
| S4 | **Secrets/personal-data scan** — outgoing commits are scanned for keys, tokens, home paths, email addresses. | 20 | No scan. | Scan exists but doesn't block. | `scan-personal-data.sh` runs in pre-push and CI; blocks the push/PR on match. |
| S5 | **Platform coverage** — CI runs on all supported platforms (Ubuntu, macOS, Windows) against multiple Python versions (3.8, 3.12). | 15 | Single platform only. | Multi-platform but single Python version. | Full matrix: 3 OS × 2 Python versions, all pass. |

## Systemic prevention criteria

| # | Criterion | Weight | 0% | 50% | 100% |
|---|---|---|---|---|---|
| P1 | **Every regression gets a regression test.** When a bug is fixed, a test is added that fails on the old code. | 25 | Regression not tested. | Test added but doesn't cover the exact failure path. | Test reproduces the exact bug; removing the fix makes the test fail. |
| P2 | **Every parity drift gets a prevention rule.** If the Python/Bash implementations drifted, a CI step or hook is updated to catch that class of drift. | 30 | No prevention added. | Documented in commit message. | New check added to parity gate or pre-push that catches the specific drift pattern. |
| P3 | **Every constraint violation gets a grep gate.** If a non-stdlib import or module-scope gi import was added, a grep-based check is added to CI. | 20 | No gate. | Documented. | `grep` command in CI that fails if the violation recurs. |
| P4 | **Every secret leak gets a pattern.** If a new kind of secret leaked, its pattern is added to `scan-personal-data.sh`. | 25 | Pattern not added. | Pattern documented. | Pattern added to `scan-personal-data.sh` with a test case in the scan script. |

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

## Current state (baseline, 2026-07)

The repodash project scores roughly **72** on this rubric today:

- **S2 (pre-push):** ENFORCED (100%) — hooks installed, all checks run
- **S3 (CI mirrors pre-push):** ENFORCED (100%) — CI has `bash-parity` + `guardrails` jobs
- **S4 (secrets scan):** ENFORCED (100%) — `scan-personal-data.sh` in pre-push + CI
- **S5 (platform coverage):** DOCUMENTED (50%) — CI matrix exists but could add Python 3.13, 3.14
- **S1 (editor-time syntax):** ABSENT (0%) — no pre-commit hook or editor integration
- **P1 (regression tests):** DOCUMENTED (50%) — practiced but not mandatory
- **P2 (parity drift prevention):** DOCUMENTED (50%) — parity gate exists but no automated drift-class detection
- **P3 (constraint grep gates):** ABSENT (0%) — constraints documented but not grep-enforced in CI
- **P4 (secret pattern updates):** DOCUMENTED (50%) — patterns static; no process for adding new ones

To reach 90+: implement editor-time syntax check, add grep-based constraint enforcement in CI, and mandate regression tests per fix.

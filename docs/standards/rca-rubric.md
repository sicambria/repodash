# Root cause analysis rubric

When any gate fails (test, parity, pre-push, CI), the agent must perform RCA before attempting a fix. This rubric scores the quality of that analysis.

## RCA phases

### Phase 1 — Triage (scored, 25 pts)

Must complete within 5 minutes. Score each step:

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| T1 | **Reproduce the failure** in isolation. | 10 | "It fails sometimes." | Full suite re-run as one command. | Single specific command reproduces the failure deterministically. Copy the exact error message and exit code. |
| T2 | **Identify the gate** that caught it. | 8 | "Tests failed." | "A parity test failed." | Specific gate name + file:line. E.g., "`test_parity.sh` L:31 — `diff` returned non-zero" or "`TestParity.test_json_parity` L:145 — AssertionError on 'repos' key." |
| T3 | **Isolate the delta** — what changed between the last passing state and now? | 7 | Changes unknown. | `git log --oneline -5` listed but not checked. | `git diff HEAD~1` output captured. Every changed file identified. If the failure predates the last commit, `git log` traced to the breaking commit. |

### Phase 2 — Diagnosis (scored, 35 pts)

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| D1 | **Root cause hypothesis** based on evidence from Phase 1. | 10 | "I don't know." | Guess without evidence. | Hypothesis cites a specific file:line and the evidence (error message, diff) that supports it. |
| D2 | **Elimination checklist** — rule out: syntax error, import error, permissions, missing dependency, environment difference. | 7 | Not considered. | Some ruled out. | All five eliminated with evidence: `bash -n repodash`, `python3 -c "import repodash"`, `ls -l repodash`, `which jq`, `echo $SHELL`. |
| D3 | **Minimal reproduction** — a single command that reproduces the failure. | 10 | No attempt. | Full test suite run. | Single command: e.g., `python3 -m unittest tests.test_repodash.TestParity.test_json_parity -v` or `repodash.py /tmp/test_tree --json`. |
| D4 | **Regression identification** — if the failure is a regression, which commit introduced it? | 8 | Not attempted. | Manual `git log` inspection. | `git bisect` identified the exact commit, OR the `git diff` in T3 directly shows the breaking line. For non-regression failures (first implementation), this is N/A and scores 100. |

### Phase 3 — Fix + Prevent (scored, 40 pts)

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| F1 | **Fix addresses the root cause**, not just the symptom. | 10 | Symptom suppressed. | Root cause partially addressed. | Root cause eliminated. Evidence: the minimal repro from D3 now passes. |
| F2 | **Regression test added** that would have caught this failure. | 12 | No new test. | Test added but doesn't fail when the fix is reverted. | Deterministic test that fails on the old code and passes on the new code. Naming: `test_regression_<bug_description>`. |
| F3 | **Prevention measure** — an automated gate that would have blocked this at the source. | 8 | No prevention. | Documented recommendation. | Implemented gate. For Python bugs: new assertion in existing test or new `grep` in CI. For Bash bugs: new `bash -n` variant or shellcheck rule. For parity drifts: added field name to normalization or type check. For secrets: pattern added to `scan-personal-data.sh`. |
| F4 | **Gate re-verified** — the full pre-push suite passes after the fix. | 10 | Not run. | Partial (one check run). | `python3 -m unittest discover tests -v && bash tests/test_parity.sh && bash -n repodash` exits 0. |

## RCA completeness score

```
RCA score = (Phase1_score × 0.25 + Phase2_score × 0.35 + Phase3_score × 0.40) × 100
```

where `PhaseN_score = Σ (criterion_weight × level) / total_phase_weight`.

| Score | Verdict |
|---|---|
| ≥ 90 | RCA is complete. Commit the fix. |
| 70–89 | RCA is adequate but has gaps. Fill in the missing evidence before committing. |
| < 70 | RCA is insufficient. Return to Phase 1. |

## Special RCA cases

### Gate false positive (the gate itself is wrong)

If the gate rejects valid code (e.g., `scan-personal-data.sh` matches a false positive pattern):

1. Same triage as above (T1–T3).
2. In D1, hypothesize: "The gate pattern X at file:line matches valid code Y."
3. F1: fix the gate (narrow the pattern, add an exception), NOT the code that triggered it.
4. F2: add a test case to the gate that the false positive code now passes.
5. F3: add a comment in the gate next to the narrowed pattern explaining why it was changed.

### Flaky test (passes sometimes, fails sometimes)

1. Reproduce by running the test 20×: `for i in $(seq 20); do python3 -m unittest tests.test_repodash.TestFoo.test_bar -v 2>&1 \| grep -E 'FAIL\|OK'; done`.
2. If < 5% failure rate, tag as flaky in a comment and open an issue. Do NOT block the change on a flaky test.
3. If ≥ 5% failure rate, the test is unreliable. Fix the underlying non-determinism (sleep, race condition, random seed) or rewrite the test to use deterministic fixtures.

## Worked example: Parity fails after adding branch_name field

**T1 — Reproduce:** `bash tests/test_parity.sh` → "PARITY FAILED" with diff showing `"branch_name": "main"` in Python output but absent in Bash output.

**T2 — Gate:** `tests/test_parity.sh` L:31 — `diff` non-zero.

**T3 — Delta:** `git diff HEAD~1` shows `repodash.py` L:590 added `branch_name` to `Config.to_dict()`; no change to `repodash`.

**D1 — Hypothesis:** `repodash.py` added the field but `repodash` L:200–230 (`print_json_entry()`) was not updated to emit `branch_name`.

**D2 — Eliminations:** `bash -n repodash` passes. `python3 -c "import repodash"` succeeds. Permissions OK. `which jq` → found. `echo $SHELL` → bash. All ruled out.

**D3 — Minimal repro:** `bash tests/test_parity.sh` (already minimal).

**D4 — Regression:** Yes, introduced by commit that added `branch_name` to Python but not Bash.

**F1 — Fix:** Add `"branch_name": "$branch_name"` to the JSON output in `repodash` `print_json_entry()`.

**F2 — Regression test:** The existing `test_json_parity` already catches this — no new test needed (the existing test IS the regression test).

**F3 — Prevention:** Not applicable — the parity gate already caught it (that's the prevention). Optionally, add a comment at `repodash.py` L:590: `# UPDATE repodash print_json_entry() WHEN ADDING FIELDS`.

**F4 — Gate re-verified:** `python3 -m unittest discover tests -v && bash tests/test_parity.sh && bash -n repodash` passes.

Score: T1=100, T2=100, T3=100 → Phase1=100. D1=100, D2=100, D3=100, D4=100 → Phase2=100. F1=100, F2=100, F3=50, F4=100 → Phase3=87.5.  
RCA = (100×0.25 + 100×0.35 + 87.5×0.40) = 95 → **≥90, commit the fix.**

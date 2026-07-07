# Root cause analysis rubric

When any gate fails (test, parity, pre-push, CI), the agent must perform RCA before attempting a fix. This rubric scores the quality of that analysis.

## RCA phases

### Phase 1 — Triage (5 min max)

| # | Step | Tool | Output |
|---|---|---|---|
| R1 | **Reproduce the failure** in isolation. | Run the failing command directly. | Exact error message + exit code. |
| R2 | **Identify the gate** that caught it. | Map the failure to a specific gate (parity, pre-push, CI job, test method). | Gate name + file:line. |
| R3 | **Isolate the delta** — what changed between the last passing state and now? | `git diff HEAD~1` or `git log --oneline -5`. | List of changed files + commit messages. |

### Phase 2 — Diagnosis

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| D1 | **Root cause hypothesis** based on evidence from Phase 1. | 20 | "I don't know." | Guess without evidence. | Hypothesis cites a specific file:line and the evidence (error message, diff) that supports it. |
| D2 | **Elimination checklist** — ruled out the obvious: syntax error, import error, permissions, missing dependency? | 15 | Not considered. | Some ruled out. | All five eliminated with evidence: syntax, imports, permissions, dependencies, environment. |
| D3 | **Minimal reproduction** — can you reproduce the bug with a one-liner? | 20 | No attempt. | Full test suite run. | Single command reproduces failure: e.g., `python3 -c "import repodash; ..."` or a specific `unittest` invocation. |
| D4 | **Bisection** — if the failure is a regression, identified the commit that introduced it. | 15 | Not attempted. | Manual `git log` inspection. | `git bisect` or evidence-based identification of the exact commit. |
| D5 | **Impact assessment** — what else might this break? Consumers, callers, CI, tray app. | 15 | Not considered. | Listed consumers. | Each consumer checked and verified not broken (or tagged for follow-up). |
| D6 | **Documented** — findings written in the commit message body or an issue comment. | 15 | No documentation. | Mentioned in commit. | RCA documented with: root cause, evidence, fix description, prevention measure. |

### Phase 3 — Fix + Prevent

| # | Criterion | Weight | 0% | 50% | 100% |
|---|---|---|---|---|---|
| P1 | **Fix addresses the root cause**, not just the symptom. | 25 | Symptom fix only. | Root cause partially addressed. | Root cause eliminated; evidence in test output. |
| P2 | **Regression test added** that would have caught this failure. | 30 | No new test. | Test added but not deterministic. | Deterministic test that fails on the old code and passes on the new code. |
| P3 | **Prevention measure** — a hook, CI check, or lint rule that would have blocked this at the source. | 25 | No prevention. | Documented recommendation. | Implemented gate: new grep rule in scan-personal-data.sh, new test, new CI step, or new assertion. |
| P4 | **Gate re-verified** — the full pre-push suite passes after the fix. | 20 | Not run. | Partial. | All gates pass: tests, parity, bash syntax, secrets scan. |

## RCA completeness score

```
RCA score = Σ (Phase2_weight × level + Phase3_weight × level) / total_weight × 100
```

- **≥ 90:** RCA is complete. Commit the fix.
- **70–89:** RCA is adequate but incomplete. Fill in the missing evidence before committing.
- **< 70:** RCA is insufficient. Return to Phase 1.

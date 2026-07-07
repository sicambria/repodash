# Planning rubric — Plan → Score → Improve

Every non-trivial change must pass three planning passes. Score each pass against this rubric; iterate until the pass clears its threshold before advancing.

## Pass thresholds

| Pass | Action | Minimum score |
|---|---|---|
| **Pass 1** | Draft plan | ≥ 80 |
| **Pass 2** | Architecture review (cross-check constraints, parity) | ≥ 90 |
| **Pass 3** | Final plan → approved → implement | ≥ 95 |

## Criteria (Pass 1 — Draft, 100 pts)

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| P1 | **Problem statement** — one sentence, falsifiable, references a test case or observed output. | 10 | No statement. | Statement exists but references the wrong behavior. | Statement is precise and falsifiable. Evidence: `python3 -c "..."` reproduces the issue. |
| P2 | **Change hypothesis** — what contract, interface, or structure must change, and why? | 10 | No hypothesis. | "Change X to fix Y" without rationale. | Hypothesis identifies the file:line to change AND explains why that change resolves P1. NOT a root cause (that's RCA) — just what code change is proposed. |
| P3 | **Impact map** — every file/function/API contract this touches, including callers. | 15 | No map. | Partial list of files. | Complete list of files, functions, and callers touched. Use `grep` to find all call sites. |
| P4 | **Parity analysis** — does this touch the JSON model? Both implementations? | 15 | Not checked. | Mentioned "yes/no" without details. | Explicit: "touches JSON model at field X → must update `repodash.py` L:N and `repodash` L:N." Cite evidence from `grep` across both files. |
| P5 | **Constraint check** — stdlib-only? gi imports inside run_gui()? Pure function contract? Bash syntax? | 10 | Not checked. | Mentioned. | Each constraint checked with a yes/no AND the evidence command from [`code-review-rubric.md`](./code-review-rubric.md) C1–C4. |
| P6 | **Test plan** — existing tests, tests to update, new tests to write. | 15 | No test plan. | Vague ("add tests"). | Specific: "`test_foo` in `test_repodash.py` L:N must now assert X; add `test_bar` for edge case Y." |
| P7 | **Implementation order** — step-by-step with a verification gate between each. | 15 | No order. | Steps listed but no gates. | Each step names the file(s) to edit and has a concrete verification command between steps. |
| P8 | **Rollback plan** — how to revert if the change breaks something. | 10 | No plan. | "git revert." | Specific files to restore, order of operations, and the command to re-verify after rollback. |

## Criteria (Pass 2 — Architecture review, 100 pts)

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| A1 | **Parity contract** — both implementations converge on the same JSON schema. | 20 | Not verified. | Hypothesized. | `bash tests/test_parity.sh` passes BEFORE any changes are made. |
| A2 | **Hard constraints** — no new imports, no config object passed to pure functions. | 20 | Not checked. | Claims checked. | Each evidence command from C1–C4 executed; output confirms compliance. |
| A3 | **Module boundaries** — how the change fits into Python/Bash/tray/tests and their interfaces. | 15 | No analysis. | Text description. | ASCII diagram naming each module, the data flowing between them, and what the change adds/removes. |
| A4 | **Backward compatibility** — does the change break the tray app, any subprocess caller, or CI? | 15 | Not considered. | Mentioned. | Each consumer listed with a yes/no verdict and the test/command used to verify. |
| A5 | **Performance impact** — does this add a subprocess call, a scan, or a blocking operation? | 10 | Not considered. | Mentioned. | Timed benchmark (e.g., `time python3 repodash.py $TREE --json`) or rationale citing existing benchmarks. |
| A6 | **Security impact** — does this introduce path traversal, shell injection, or config leakage? | 20 | Not considered. | Mentioned. | Each vector checked with a yes/no; for shell changes, cite the exact line and verify it uses arrays or proper quoting. |

## Criteria (Pass 3 — Final, 100 pts)

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| F1 | **Test coverage** — all new code paths have a deterministic test. | 30 | No new tests. | Some new tests. | Every code path introduced by the change has a test. Remove the change → the new test fails. |
| F2 | **Parity gate passes.** | 30 | Not run. | Run but failing. | `bash tests/test_parity.sh` passes with zero diff. |
| F3 | **Full suite passes.** | 20 | Not run. | Run but failing. | `python3 -m unittest discover tests -v` passes, zero failures. |
| F4 | **Final verification gate** — all three checks pass in sequence. | 20 | Not run. | Run but one fails. | `python3 -m unittest discover tests -v && bash tests/test_parity.sh && bash -n repodash` exits 0. |

## Scoring formula

```
Pass N score = Σ (criterion_weight × level) / total_criterion_weight × 100
```

where `level` is 0.0, 0.5, or 1.0 (Absent=0, Documented=0.5, Enforced=1.0).

## Iteration rule

If a pass scores below its threshold, identify the 2–3 lowest-scoring criteria, revise the plan targeting those weaknesses, and re-score. Continue until the threshold is met.

**Escalation.** If Pass 1 requires more than 3 iterations, escalate: the problem may be too large. Split the change into two smaller, independently-plannable changes and re-enter Phase 1 for each.

## Worked example: Adding a "branch_name" field to the JSON model

**P1 — Problem statement:** The tray dashboard shows repo names but not the active branch. Users must open each terminal to see the branch. No existing test asserts branch_name in the JSON.

**P2 — Change hypothesis:** Add a `branch_name` string field to `Config.to_dict()` at `repodash.py` L:590 and echo it in `repodash` L:210 so both `--json` outputs include it. The tray already parses `repos[]` dicts — no tray change needed.

**P3 — Impact map:**
- `repodash.py`: `Config.to_dict()` (L:590), `scan_dirty()` call site where branch is resolved (L:400–420)
- `repodash`: `print_json_entry()` function (L:200–230), git branch resolution (L:140–155)
- `tests/test_repodash.py`: `TestParity.test_json_parity` — field list assertion; `TestModel.test_schema_keys_present` — schema assertion
- `tests/test_parity.sh`: normalization function — add `branch_name` to ignored fields? No, it should MATCH.

**P5 — Constraint check:**
- C1 (stdlib): no new imports → ✅
- C2 (gi): N/A (not touching tray) → ✅
- C3 (pure functions): `scan_dirty()` already resolves git branch → ✅
- C4 (bash syntax): will run `bash -n` after edit → ✅

**Score (Pass 1):** P1=100, P2=100, P3=100, P4=50 (yes, but didn't cite exact lines yet), P5=100, P6=50 (vague test plan), P7=0 (no implementation order listed), P8=50 (only "git revert").  
Sum: (10+10+15+7.5+10+7.5+0+5) / 100 = 65/100 → **FAIL.**  
Iteration needed: cite exact lines in both implementations for P4, write specific test method names for P6, list step-by-step order for P7.

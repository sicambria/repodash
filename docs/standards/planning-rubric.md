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
| P1 | **Problem statement** is one sentence stating *what* breaks or *what* must change. | 10 | No statement. | Statement exists but references the wrong behavior. | Statement is precise, falsifiable, and references a test case or observed output. |
| P2 | **Root cause hypothesis** — what line/file/contract is the origin? | 10 | No hypothesis. | Hypothesis exists but unsupported. | Hypothesis cites a file:line and linked evidence (test output, diff, log). |
| P3 | **Impact map** — every file/function/API contract this touches. | 15 | No map. | Partial list of files. | Complete list of files and functions touched, including callers. |
| P4 | **Parity analysis** — does this touch the JSON model? Both implementations? | 15 | Not checked. | Mentioned "yes/no" without details. | Explicit: "touches JSON model at field X → must update repodash.py L:N and repodash L:N." |
| P5 | **Constraint check** — stdlib-only? gi imports inside run_gui()? Pure function contract? | 10 | Not checked. | Mentioned. | Each constraint checked with a yes/no and evidence. |
| P6 | **Test plan** — existing tests, tests to update, new tests to write. | 15 | No test plan. | Vague ("add tests"). | Specific: "test_foo in test_repodash.py L:N must assert X; add test_bar for edge case Y." |
| P7 | **Implementation order** — step-by-step with verification gate between each. | 15 | No order. | Steps listed but no gates. | Each step has a verification command (e.g., `python3 -m unittest tests.test_repodash.TestParity -v`). |
| P8 | **Rollback plan** — how to revert if the change breaks something. | 10 | No plan. | "git revert." | Specific: "revert commit X; if that fails, restore files A, B, C and re-run scripts/install-hooks.sh." |

## Criteria (Pass 2 — Architecture review, 100 pts)

| # | Criterion | Weight | 0% | 50% | 100% |
|---|---|---|---|---|---|
| A1 | **Parity contract** is verified — both implementations converge on the same JSON schema. | 20 | Not verified. | Hypothesized. | Diff of normalized outputs confirmed identical before implementing. |
| A2 | **Hard constraints** are proved satisfied — no new imports, no config object passed to pure functions. | 20 | Not checked. | Claims checked. | Evidence: `grep` output confirming zero new imports, function signatures unchanged. |
| A3 | **Architecture diagram** — how the change fits into the existing module boundaries (Python/Bash/tray/tests). | 15 | No diagram. | Text description. | ASCII diagram or bullet map showing data flow across modules. |
| A4 | **Backward compatibility** — does the change break any existing consumer (tray, subprocess callers, CI)? | 15 | Not considered. | Mentioned. | List of consumers checked, each with a yes/no + rationale. |
| A5 | **Performance impact** — does this add a subprocess call, a scan, or a blocking operation? | 10 | Not considered. | Mentioned. | Timed benchmark or rationale for why it's negligible. |
| A6 | **Security impact** — does this introduce path traversal, shell injection, or config leakage? | 20 | Not considered. | Mentioned. | Each vector checked with a yes/no; shell commands use arrays, paths are sanitized. |

## Criteria (Pass 3 — Final, 100 pts)

| # | Criterion | Weight | 0% | 50% | 100% |
|---|---|---|---|---|---|
| F1 | **Test coverage** — all new paths have a test. | 30 | No new tests. | Some new tests. | Every code path introduced by the change has a deterministic test. |
| F2 | **Parity gate passes** before the change is committed. | 30 | Not run. | Run but failing. | `bash tests/test_parity.sh` passes. |
| F3 | **Full suite passes** — `python3 -m unittest discover tests -v`. | 20 | Not run. | Run but failing. | All tests pass, zero failures. |
| F4 | **Pre-push gate simulates clean** — `bash -n repodash` + parity + secrets scan. | 20 | Not checked. | Checked manually. | `scripts/git-hooks/pre-push` executes clean (or equivalent manual checks). |

## Scoring formula

```
Pass N score = Σ (criterion_weight × level) for all criteria in Pass N
```

where `level` is 0.0, 0.5, or 1.0 (Absent=0, Documented=0.5, Enforced=1.0).

## Iteration rule

If a pass scores below its threshold, identify the lowest-scoring criteria, revise the plan targeting those, and re-score. Continue until the threshold is met.

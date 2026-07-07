# repodash standards & rubrics

> **Three-level rule** (inherited from `inventory/world-class-inventory-standard.md`):  
> | Level | Credit | Objective definition |
> |---|---|---|
> | **Absent / Red** | 0% | The property is violated, or no artifact exists for it. |
> | **Documented** | 50% | The property holds and an artifact exists, **but no gate enforces it.** |
> | **Enforced** | 100% | The property holds **and** a git hook or CI workflow fails a regression. |

## Rubric index

| Rubric | Scope | Phase | Weight |
|---|---|---|---|
| [`planning-rubric.md`](./planning-rubric.md) | Plan → Score → Improve cycle (three-pass) | 1, 2, 3 | 25 |
| [`code-review-rubric.md`](./code-review-rubric.md) | Code quality, constraint adherence | 2, 3 | 25 |
| [`parity-rubric.md`](./parity-rubric.md) | JSON parity contract | 2, 3 | 20 |
| [`rca-rubric.md`](./rca-rubric.md) | Root cause analysis when gates fail | After any failure | 15 |
| [`prevention-rubric.md`](./prevention-rubric.md) | Shift-left detection, systemic prevention | 3, post-fix | 15 |

## Phase-to-rubric mapping

| Workflow Phase | Rubrics to score |
|---|---|
| **Phase 1** — Draft plan | Planning (Pass 1 criteria) |
| **Phase 2** — Architecture review | Planning (Pass 2) + Code Review + Parity |
| **Phase 3** — Implement & verify | Planning (Pass 3) + Code Review + Parity + Prevention |
| **Any gate failure** | RCA |

## Combined quality score

Before any change lands on `main`, compute the combined score:

```
Combined = Σ (rubric_weight × rubric_score) / 100
```

| Combined score | Verdict |
|---|---|
| ≥ 95 | **SOTA** — Change is ready to commit. |
| 85–94 | **Strong** — Commit if urgency demands; add follow-up ticket for gaps. |
| < 85 | **Insufficient** — Iterate on lowest-scoring rubrics. Do not commit. |

## Quick-start

**New contributor?** Read in order: Planning → Code Review → Parity → AGENTS.md.  
**About to make a change?** Start at Phase 0 in AGENTS.md.  
**Gate just failed?** Jump to RCA rubric, Phase 1.

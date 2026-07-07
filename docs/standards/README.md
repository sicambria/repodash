# repodash standards & rubrics

> **Three-level rule** (inherited from `inventory/world-class-inventory-standard.md`):  
> | Level | Credit | Objective definition |
> |---|---|---|
> | **Absent / Red** | 0% | The property is violated, or no artifact exists for it. |
> | **Documented** | 50% | The property holds and an artifact exists, **but no gate enforces it.** |
> | **Enforced** | 100% | The property holds **and** a git hook or CI workflow fails a regression. |

| Rubric | Scope |
|---|---|
| [`planning-rubric.md`](./planning-rubric.md) | Plan → Score → Improve cycle (three-pass minimum) |
| [`code-review-rubric.md`](./code-review-rubric.md) | Code quality, constraint adherence, test coverage |
| [`parity-rubric.md`](./parity-rubric.md) | JSON parity contract between Python and Bash |
| [`rca-rubric.md`](./rca-rubric.md) | Root cause analysis when gates fail |
| [`prevention-rubric.md`](./prevention-rubric.md) | Shift-left detection, automated enforcement, systemic prevention |

**Scorecard.** Before any change lands on `main`, the agent must self-score against every rubric. The combined score is a weighted floor — below the floor, iterate before implementing.

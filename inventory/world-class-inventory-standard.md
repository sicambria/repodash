# World-Class Repo Inventory Standard (Cross-Repo Score, /1000)

**Status:** Current standard.
**Created:** 2026-07-03.
**Home:** `~/git/repodash/inventory/` (script `world-class-inventory.mjs`; output `~/git/INDEX.MD`).
**Goal:** Produce a *reproducible* 0–1000 "world-class" score for **every repository under `~/git`**, from
**externally-observable signals only** — so any operator (human or AI) re-running the script arrives at the
same number without running each repo's private toolchain.

---

## Relationship to the changemappers rubric

This standard deliberately **mirrors the *form*** of changemappers'
`docs/standards/codebase-health-rubric.md` — evidence-command per criterion, the
**Absent (0%) / Documented (50%) / Enforced (100%)** three-level rule, additive to **1000**, band read
off afterward — but it does **not** port that rubric's *criteria*. Those resolve to npm scripts run *inside*
changemappers (`typecheck`, `vitest --coverage`, `cleancode:check`) and cannot run uniformly across a
research paper or a shell-script repo. Instead this rubric scores signals observable **from outside** any
repo: git hygiene, git hooks, CI workflows, docs, standards, roadmap discipline, Sonar API stats, test
config, audit-report coverage, lint/clean-code config.

changemappers is the **gold standard** and the intended ceiling of the *absolute* scale. Weights are **not**
reverse-engineered to force it to exactly 1000 — criteria are defined on their merits and its score falls
out (validate by ranking, below).

## The three-level rule (inherited verbatim)

| Level | Credit | Objective definition |
| --- | ---: | --- |
| **Absent / Red** | 0% | The property is violated, or no artifact exists for it. |
| **Documented** | 50% | The property holds and an artifact exists, **but no gate (git hook or CI job) enforces it.** |
| **Enforced** | 100% | The property holds **and** a git hook or CI workflow would fail a regression. |

## Repo discovery rule (pinned for reproducibility)

A directory `D` directly under `~/git` is **in scope** iff `D/.git` is a **directory** (a real clone). This
deliberately **excludes**: git-*file* worktree siblings, `*-wt` / `*-worktrees` containers, and non-git
directories (`cross-repo`, `ai-meta`, `*-paper` folders that are not clones). As of 2026-07-03 this yields
**23 repos**. Override the scan root with `REPODASH_DIR`.

## Repo type classification (rule-based)

| Order | Type | Rule (first match wins) |
| --: | --- | --- |
| 1 | **code** | A language manifest: `package.json`, `pyproject.toml`/`setup.py`, `go.mod`, `Cargo.toml`. |
| 2 | **research** | ≥ 1 `.tex`, ≥ 3 `.ipynb`, or a `paper/`/`manuscript/` dir. (Checked *before* the source-count fallback so a paper repo with figure/build scripts — and a `Makefile` — still reads as research.) |
| 3 | **code** | ≥ 10 source files (code extensions **or** extensionless `#!` shebang scripts), **or** ≥ 4 source files with a `tests/` dir of test scripts. Catches manifest-less CLI tools (e.g. `repodash`). |
| 4 | **other** | Everything else (notes, configs, utility dumps). |

> `Makefile` is deliberately **not** a code signal — LaTeX papers, docs, and infra all use one.

`code` repos are scored on all 1000 points. `research`/`other` repos have the **code-specific block (500)**
marked **N/A**; their type-adjusted score uses only the applicable (universal) denominator.

---

## The two scores

- **Absolute /1000** — raw earned points over the fixed 1000. changemappers = the ceiling. A research repo
  will read low here **by design** — it cannot earn the code-specific 500. This is the "how world-class,
  literally" column.
- **Type-adjusted /1000** — `earned ÷ (Σ applicable maxima) × 1000`. Lets a pristine paper repo read as
  world-class *for what it is*. Both fall out of the same criteria.

### Bands (shared with changemappers)

| Band | Range |
| --- | ---: |
| Ad hoc | 0–199 |
| Managed | 200–399 |
| Defined | 400–599 |
| Quantitatively managed | 600–799 |
| World-class | 800–1000 |

---

## Universal dimensions — apply to every repo (500)

### U1 — Version-control & repo hygiene (80)
| # | Criterion | Max | Evidence |
| --- | --- | ---: | --- |
| U1.1 | Has commit history (≥ 1 commit). | 15 | `git rev-list --count HEAD` |
| U1.2 | README present and substantial (≥ 400 bytes). | 20 | `README*` size |
| U1.3 | LICENSE present. | 15 | `LICENSE*` |
| U1.4 | `.gitignore` present. | 10 | file exists |
| U1.5 | Working tree not drowning in uncommitted change (< 50 dirty paths). | 20 | `git status --porcelain \| wc -l` |

### U2 — Documentation & knowledge (100)
| # | Criterion | Max | Evidence |
| --- | --- | ---: | --- |
| U2.1 | `docs/` directory exists. | 25 | dir exists |
| U2.2 | Agent/contributor guide (`AGENTS.md` or `CLAUDE.md`). | 25 | file exists |
| U2.3 | `CONTRIBUTING.md` present. | 20 | file exists |
| U2.4 | Doc index / TOC (`docs/**/DOC_TOC.md`, `docs/INDEX*.md`, `docs/README.md`). | 30 | file exists |

### U3 — CI / CD automation (110)
| # | Criterion | Max | Evidence |
| --- | --- | ---: | --- |
| U3.1 | ≥ 1 CI workflow. | 40 | `.github/workflows/*.yml` count ≥ 1 |
| U3.2 | ≥ 3 workflows (separated concerns). | 40 | count ≥ 3 → full; 1–2 → half |
| U3.3 | A security / quality workflow exists (name matches `security\|codeql\|sonar\|quality\|audit`). | 30 | filename/content match |

### U4 — Guardrails / git hooks (110)
| # | Criterion | Max | Evidence |
| --- | --- | ---: | --- |
| U4.1 | A `pre-commit` hook exists (husky or `.git/hooks`). | 40 | `.husky/pre-commit` or hook file |
| U4.2 | A `pre-push` hook exists. | 40 | `.husky/pre-push` |
| U4.3 | Hooks actually run checks (lint/test/typecheck/guard token in hook body). | 30 | grep hook bodies |

### U5 — Roadmap & planning discipline (50)
| # | Criterion | Max | Evidence |
| --- | --- | ---: | --- |
| U5.1 | A roadmap exists (`ROADMAP.md`, `docs/roadmap/*`, `TODO.md`, `BACKLOG.md`). | 25 | file exists |
| U5.2 | Plans/RCA discipline (`docs/plans/` or `docs/errors/` present). | 25 | dir exists |

### U6 — Standards & governance (50)
| # | Criterion | Max | Evidence |
| --- | --- | ---: | --- |
| U6.1 | `docs/standards/` exists with ≥ 1 standard. | 25 | count ≥ 1 |
| U6.2 | ≥ 5 standards (a governance corpus). | 25 | count ≥ 5 → full; 1–4 → half |

---

## Code-specific dimensions — N/A for research/other (500)

### C1 — Sonar onboarding & gate (110)
| # | Criterion | Max | Evidence |
| --- | --- | ---: | --- |
| C1.1 | `sonar-project.properties` present. | 30 | file exists |
| C1.2 | Project exists in the Sonar server & is analysed. | 30 | `/api/measures/component` returns measures |
| C1.3 | Quality gate = OK. | 30 | `alert_status = OK` |
| C1.4 | Gate is push-enforced (`sonar` token in a hook, or no `.sonar-optout`). | 20 | grep hooks / `.sonar-optout` |

### C2 — Sonar issue posture & coverage (130)
| # | Criterion | Max | Evidence |
| --- | --- | ---: | --- |
| C2.1 | bugs = 0. | 25 | measure `bugs` |
| C2.2 | vulnerabilities = 0. | 25 | measure `vulnerabilities` |
| C2.3 | code_smells = 0. | 20 | measure `code_smells` |
| C2.4 | security_hotspots = 0. | 20 | measure `security_hotspots` |
| C2.5 | coverage band (≥ 80% full · 55–80% half · < 55% none). | 40 | measure `coverage` |

### C3 — Test infrastructure (110)
| # | Criterion | Max | Evidence |
| --- | --- | ---: | --- |
| C3.1 | A test runner is configured (`vitest`/`jest`/`playwright`/`pytest`/`go test`). | 35 | config file / `package.json` |
| C3.2 | Test files exist (`*.test.*`, `*.spec.*`, `test_*.py`, `*_test.go`). | 40 | count ≥ 1 |
| C3.3 | Coverage tooling configured (coverage config / `--coverage` script / `.coveragerc`). | 35 | config/script match |

### C4 — Audit-standard coverage (80)
Canonical audit catalog (from changemappers `docs/standards/*audit-standard*.md` + the compliance/health/
maturity assessments): **tooling, database, registration, route, cloudflare** audits, plus **codebase-health**,
**ai-framework-maturity**, and **compliance** (ISO/SOC/NIST) assessments — 8 canonical audits.
An audit is **"run"** for a repo iff a report file under `docs/audits/**` (or `docs/meta/**`) names it.
| # | Criterion | Max | Evidence |
| --- | --- | ---: | --- |
| C4.1 | ≥ 1 canonical audit has a report. | 40 | grep `docs/audits/**` |
| C4.2 | ≥ 4 canonical audits have reports. | 40 | count ≥ 4 → full; 1–3 → prorated half |

### C5 — Lint & clean-code enforcement (70)
| # | Criterion | Max | Evidence |
| --- | --- | ---: | --- |
| C5.1 | Lint config present (`eslint.config.*`, `.eslintrc*`, `ruff.toml`, `.flake8`). | 25 | file exists |
| C5.2 | Clean-code / duplication config (`.jscpd.json`, `*budgets*baseline*`, `stryker.conf*`). | 20 | file exists |
| C5.3 | Lint is enforced (lint token in a hook or CI workflow). | 25 | grep hooks/workflows |

---

## Scoring procedure

1. For each criterion run its evidence check; assign 0 / 50 / 100 % of its max per the three-level rule.
2. Sum per dimension; sum dimensions → **absolute /1000**.
3. For research/other, exclude the C-block from the denominator → **type-adjusted /1000**.
4. Read the band off each score.
5. The script emits both scores + every signal to `~/git/inventory.json`; `INDEX.MD` is rendered from it.

## Honesty caveats

- **Point-in-time snapshot.** A few criteria read *live* state — U1.1 (commit count), U1.5 (git-dirty
  paths), and all Sonar measures (coverage especially). "Same evidence → same number" holds for a **fixed
  working-tree + Sonar snapshot**; these values drift between runs *by design* (that is what makes it a live
  dashboard). Everything else is a deterministic function of files on disk.
- **Self-assessment.** This rubric is derived from changemappers' own standards catalog, so changemappers
  topping the scale is partly tautological — the same caveat its own `ai-framework-maturity-standard.md`
  flags ("the system checking its own work"). The score is a **reproducible internal comparison**, not an
  independent certification.

## Validate by ranking (calibration guard)

After a run, **changemappers must top the absolute list**, with the Tier-1 apps (knowyourself, metalearner,
coachcompanion — all Sonar authored-zero) clustered just below. If they do not, the **rubric** is
miscalibrated, not the repos. Do not adjust weights to hit a target number.

## How to run

```bash
node ~/git/repodash/inventory/world-class-inventory.mjs   # writes ~/git/INDEX.MD + ~/git/inventory.json
```

Sonar stats need the local server (`http://localhost:9000`) up; the script reads each onboarded repo's own
`SONAR_TOKEN` from its `.env`. If Sonar is down, C1.2/C1.3/C2.* score 0 and the run is flagged
`sonar: unreachable` (re-run when up rather than trusting the degraded numbers).

## Version history

- `2026-07-03`: Created. First cross-repo application rendered to `~/git/INDEX.MD`.

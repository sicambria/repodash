# Parity rubric — JSON contract enforcement

`repodash.py --json` and `repodash --json` must emit semantically identical JSON for the same inputs. This rubric governs that contract.

## Parity dimensions (each scored)

| # | Dimension | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| Y1 | **Schema definition** — the canonical schema lives in `repodash.py` (the `Config` model and JSON output). | 15 | No canonical schema. | Schema exists in one implementation only. | Schema is defined in `repodash.py:Config` and matched exactly in `repodash` (Bash). |
| Y2 | **Field completeness** — every field in the Python output has a corresponding field in the Bash output. | 20 | Fields missing in one implementation. | Partial match. | `diff <(normalize "$py_json") <(normalize "$sh_json")` produces zero output. |
| Y3 | **Field types** — booleans are booleans, integers are integers, nulls are nulls in both outputs. | 20 | Type mismatch exists. | Types match but null handling differs. | Every field has identical JSON type in both outputs. |
| Y4 | **Sort order** — the `repos` array is deterministically ordered in both implementations. | 15 | Order differs between runs. | Stable per-run but differs between implementations. | Both sort by the same key; `repos` arrays are identical at the same index. |
| Y5 | **Normalization** — volatile fields (`generated_at`, `base_dir`) are excluded from parity comparison; paths are relativized. | 15 | Raw comparison without normalization. | Normalization done but manual. | `test_parity.sh` normalizes automatically; `TestParity` does the same in unittest. |
| Y6 | **Both tests enforce it** — the parity gate runs in both the bash script and the Python test suite. | 15 | Only one test exists. | Both exist but one is skipped in CI. | `bash tests/test_parity.sh` AND `python3 -m unittest tests.test_repodash.TestParity` both run in CI and pre-push. |

## Parity failure RCA protocol

When parity fails:

```
1. diff the normalized JSON:  bash tests/test_parity.sh  (prints diff to /tmp/repodash_parity.diff)
2. identify the field:        read the diff → which field differs?
3. trace to implementation:   grep for that field in both repodash.py and repodash
4. determine root cause:      is it a schema change not ported? a type mismatch? a sort order divergence?
5. fix the lagging implementation, then re-run the parity gate
6. document the root cause:   add a line to the commit message body explaining what drifted and why
```

## Prevention checklist (before any commit that touches the JSON model)

```
□ Does this change add, remove, or rename a JSON field?     → Update both implementations.
□ Does this change alter the type of an existing field?       → Update both implementations.
□ Does this change affect sort order?                         → Verify both sort by the same key.
□ Did I run tests/test_parity.sh?                             → Must pass.
□ Did I run python3 -m unittest tests.test_repodash.TestParity? → Must pass.
```

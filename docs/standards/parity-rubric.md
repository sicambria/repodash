# Parity rubric — JSON contract enforcement

`repodash.py --json` and `repodash --json` must emit semantically identical JSON for the same inputs. This rubric governs that contract.

## Parity dimensions (each scored)

| # | Dimension | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) |
|---|---|---|---|---|---|
| Y1 | **Schema definition** — the canonical schema lives in `repodash.py`. | 15 | No canonical schema. | Schema exists in one implementation only. | Schema is defined in `repodash.py:Config` and matched exactly in `repodash` (Bash). Evidence: `python3 -c "from repodash import Config; print(Config.__annotations__)"` lists every field, and every field appears in Bash output. |
| Y2 | **Field completeness** — every field in the Python output has a corresponding field in the Bash output, and vice versa. | 20 | Fields missing in one implementation. | Partial match (one extra/missing field). | `diff <(normalize "$py_json") <(normalize "$sh_json")` produces zero output for the same fixture tree. |
| Y3 | **Field types** — booleans are booleans, integers are integers, nulls are nulls in both outputs. | 20 | Type mismatch exists. | Types match but null representation differs (e.g., `None` vs `null`). | Every field has identical JSON type in both outputs. Verify with `python3 -c "import json; d1=json.load(open('py.json')); d2=json.load(open('sh.json')); [print(k, type(v1).__name__, type(v2).__name__) for k in d1 if type(d1[k]) != type(d2[k])]"` — produces zero output. |
| Y4 | **Sort order** — the `repos` array is deterministically ordered in both implementations. | 15 | Order differs between runs. | Stable per-run but differs between implementations. | Both sort by the same key; `repos[i]` in Python matches `repos[i]` in Bash at every index. Evidence: run both implementations 5 times — sort order is identical across all 10 outputs. |
| Y5 | **Volatile field exclusion** — fields that differ by nature (`generated_at` timestamp, `base_dir` absolute path) are excluded from parity comparison. | 15 | Raw comparison includes volatile fields → false failures. | Volatile fields documented but normalization done manually. | The normalization step in `test_parity.sh` and `TestParity` strips volatile fields before comparison. Evidence: run parity with different timestamps/paths — still passes. |
| Y6 | **Dual enforcement** — parity is checked in both the bash script and the Python test suite. | 15 | Only one test exists. | Both exist but one is skipped in CI. | `bash tests/test_parity.sh` AND `python3 -m unittest tests.test_repodash.TestParity -v` both run and pass in CI. Evidence: `.github/workflows/ci.yml` includes both the `bash-parity` job (which runs `test_parity.sh`) and the `python` job (which runs `TestParity`). |

## Parity failure RCA protocol

When parity fails:

```
1. diff the normalized JSON:  bash tests/test_parity.sh  (prints diff to /tmp/repodash_parity.diff)
2. identify the field:        read the diff → which field differs?
3. trace to implementation:   grep for that field in both repodash.py and repodash
4. determine root cause:      is it a schema change not ported? a type mismatch? a sort order divergence?
5. fix the lagging implementation, then re-run the parity gate
6. document the root cause:   add a RCA block to the commit message body
```

## Prevention checklist (before any commit that touches the JSON model)

```
□ Does this change add, remove, or rename a JSON field?     → Update both implementations.
□ Does this change alter the type of an existing field?       → Update both implementations.
□ Does this change affect sort order?                         → Verify both sort by the same key.
□ Did I run bash tests/test_parity.sh?                        → Must pass with zero diff.
□ Did I run python3 -m unittest tests.test_repodash.TestParity -v? → Must pass.
```

## Extending the parity gate (when adding a new field)

1. Add the field to `Config.to_dict()` in `repodash.py`.
2. Add the matching output to `print_json_entry()` in `repodash` (Bash).
3. If the field is volatile (timestamp, path), add it to the normalization step in `test_parity.sh` and in `TestParity.test_json_parity`.
4. Run the full verification gate: `python3 -m unittest discover tests -v && bash tests/test_parity.sh && bash -n repodash`.
5. Update the schema assertion in `TestModel.test_schema_keys_present` to include the new field.

## Schema versioning

If the JSON schema changes in a backward-incompatible way (field removed, renamed, or type changed), bump the version field in `Config.to_dict()` and in the Bash output. Consumers (tray, external scripts) can check `version` to decide whether to parse.

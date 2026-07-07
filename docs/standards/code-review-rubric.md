# Code review rubric

Every change must pass this rubric before landing on `main`. Score at 0%, 50%, or 100% per criterion.

## Hard constraints (pass/fail — any failure blocks the change)

| # | Constraint | Evidence command | Fail = block |
|---|---|---|---|
| C1 | **stdlib-only.** `repodash.py` and `tray/repodash_tray.py` import only from the Python standard library. | `python3 -c "import sys; sys.path[:] = [p for p in sys.path if 'site-packages' not in p and 'dist-packages' not in p]; import repodash"` — must succeed without ImportError. Also: `grep -nE '^import |^from ' repodash.py tray/repodash_tray.py \| grep -vE '"'"'(sys|os|json|re|subprocess|shutil|tempfile|unittest|dataclasses|typing|enum|argparse|datetime|time|math|collections|functools|itertools|pathlib|hashlib|base64|hmac|http|urllib|xml|html|logging|traceback|threading|queue|signal|socket|select|struct|textwrap|string|io|csv|random|stat|grp|pwd|platform|getpass|configparser|webbrowser|importlib|inspect|warnings|atexit|cgitb|pdb|profile|trace|dis|tokenize|linecache|pprint|glob|fnmatch|gzip|zipfile|tarfile|bz2|lzma)'"'"'` — must produce zero output. | Any non-stdlib import detected. |
| C2 | **gi imports inside run_gui().** Every `gi`/`Gtk`/`AppIndicator` import in `tray/repodash_tray.py` is inside `def run_gui():`. | `python3 -c "import ast; t = ast.parse(open('tray/repodash_tray.py').read()); imports = [(n.lineno, n.names[0].name) for n in ast.walk(t) if isinstance(n, (ast.Import, ast.ImportFrom))]; gi_imports = [(l,n) for l,n in imports if any(x in n.lower() for x in ('gi','gtk','appindicator'))]; func = [n for n in ast.walk(t) if isinstance(n, ast.FunctionDef) and n.name == 'run_gui']; print('FAIL' if gi_imports and func and any(l < func[0].lineno for l,_ in gi_imports) else 'OK')"` | "FAIL" output. |
| C3 | **find_repos() and scan_dirty() are pure.** They take no config object. | `grep -n 'def find_repos\|def scan_dirty' repodash.py` — verify signatures contain only `(root_dir, ...)` parameters, never a `config` or `cfg` parameter. | Either function accepts a config parameter or dict. |
| C4 | **Bash syntax passes.** | `bash -n repodash` | Non-zero exit. |

## Code quality (scored)

| # | Criterion | Weight | 0% (Absent) | 50% (Documented) | 100% (Enforced) | Evidence command |
|---|---|---|---|---|---|---|
| Q1 | **Function size** — ≤50 lines per function. | 15 | Functions >100 lines. | Functions ≤100 lines. | Functions ≤50 lines; complex logic extracted to named helpers. | `grep -n '^def \|^    def ' repodash.py \| awk -F: '{print $1}' \| while read start; do awk "NR>=$start && /^[^ ]/{if(NR>$start+1) exit} {print NR}" repodash.py \| tail -1; done` — every function body ≤50 lines between `def` and next top-level token. |
| Q2 | **Single responsibility** — each function does one thing named by its signature. | 15 | Functions mix I/O, logic, and formatting in one block. | Separated but named vaguely. | Each function has a clear, single purpose evident from its name. Verifiable by reading the function's docstring/first line against its body. | Manual review. |
| Q3 | **No dead code** — no commented-out blocks, unused imports, unreachable branches. | 15 | Dead code present in changed files. | Removed but one orphaned import remains. | Zero dead code in changed files. | `grep -nE '^[[:space:]]*#.*(TODO|FIXME|HACK|XXX)|^[[:space:]]*#[^ ]*$' repodash.py tray/repodash_tray.py` — zero matches in changed lines. |
| Q4 | **Naming consistency** — follows existing conventions. | 10 | Inconsistent names. | Mostly consistent. | Every new name matches the file's style (Python: `snake_case`; Bash: `snake_case`). | Manual review. |
| Q5 | **Error handling** — errors propagate with context; no bare `except:` or `\|\| true` swallowing. | 15 | Swallowing without logging. | Errors logged but swallowed. | Errors bubble up or are handled with a stderr message + non-zero exit. | `grep -n 'except:' repodash.py tray/repodash_tray.py` — zero bare `except:` matches. For Bash: `grep -n '|| true' repodash` — zero "swallow all errors" patterns. |
| Q6 | **Shell safety** — `set -euo pipefail` at top of any new Bash code; no unquoted variable expansions. | 15 | Unsafe shell in changed code. | `set -eu` but no pipefail. | `set -euo pipefail` + all variable expansions inside double quotes. | `shellcheck repodash` or manual review of `$var` vs `"$var"` in changed lines. |
| Q7 | **Test style** — tests use `fixtures.py`, are deterministic, and assert one behavior each. | 15 | Tests rely on real repos or randomness. | Uses fixtures but groups multiple unrelated assertions. | One conceptual assertion per test method; uses `tests/fixtures.py` exclusively. | Manual review. |

## Additional criteria for tray changes

| # | Criterion | Weight | 0% | 50% | 100% | Evidence |
|---|---|---|---|---|---|---|
| T1 | **Config consistency** — any new config key has a default in `CONFIG_DEFAULTS`, is round-tripped in `save_config`/`load_config`, and is applied in `apply_config_to_env`. | 15 | Key added to one place only. | Key in defaults and save, missing from apply. | Key in defaults, save, load, and apply. | `grep -n 'NEW_KEY' tray/repodash_tray.py` — must appear in all four locations. |
| T2 | **Flag consistency** — any new CLI flag in `repodash.py` has a matching `--flag` in `repodash` (Bash). | 15 | Flag in one implementation only. | Flag in both but behavior differs. | Flag in both implementations; test_repodash.py asserts matching behavior. | `grep -n 'add_argument.*--newflag' repodash.py` AND `grep -n '--newflag' repodash` — both must match. |

## Scoring

```
Code review score = Σ (Qn_weight × level + Tn_weight × level) / total_weight × 100
```

A score below **85** fails code review. Fix the violation and re-score.

## Worked example

A commit adds a `--depth` flag to `repodash.py` but forgets the Bash implementation:

- C1 (stdlib): ✅ no new imports → 100
- C2 (gi): N/A → 100
- C3 (pure functions): ✅ unchanged → 100
- C4 (bash syntax): ✅ passes `bash -n` → 100
- Q1–Q7: mostly clean → avg 85
- T2 (flag consistency): ❌ flag exists in Python but NOT in Bash → 0

Score: (hard constraints all pass) but quality score = (7×85 + 0) / (7+1) = 74 → **FAIL.**

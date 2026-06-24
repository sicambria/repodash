# AGENTS.md

## What this project is

repodash is a multi-repo git dashboard. It ships two implementations kept at strict JSON parity:

| File | Language | Purpose |
|---|---|---|
| `repodash.py` | Python 3.8+, stdlib only | Canonical implementation; cross-platform |
| `repodash` | Bash 3.2+ | Parity port for Unix shells |
| `tray/repodash_tray.py` | Python + GTK3 | GNOME tray icon + dashboard (Linux only) |

## Running tests

```bash
python3 -m unittest discover tests -v   # full suite
bash tests/test_parity.sh               # JSON parity gate (requires bash + jq)
```

CI runs on Ubuntu, macOS, and Windows against Python 3.8 and 3.12. All tests use stdlib `unittest` — no test dependencies to install.

## The parity invariant — do not break this

`repodash.py --json` and `repodash --json` must emit semantically identical JSON for the same inputs. The schema is defined in `repodash.py`; the Bash port must match it exactly. The parity gate (`TestParity` in `tests/test_repodash.py` and `tests/test_parity.sh`) enforces this.

**If you change the JSON model in `repodash.py`, you must make the matching change in `repodash` (Bash).** There are no exceptions.

## Hard constraints

- **No third-party packages.** `repodash.py` and `tray/repodash_tray.py` must import only from the Python standard library. Do not add `import` statements that require `pip install`.
- **All `gi` imports inside `run_gui()`.** `tray/repodash_tray.py` keeps every `gi`/`Gtk`/`AppIndicator` import inside `run_gui()` so the module loads without a display — the test suite relies on this.
- **`find_repos()` and `scan_dirty()` are pure.** They take no config object. Filtering (excluded repos, base dir, depth) is applied at the call site, not inside these functions.

## Architecture — tray app

`tray/repodash_tray.py` has two tiers:

1. **Menu tier** — cheap; runs `git status` per repo on a background thread every ~90s. Uses `scan_dirty()` → filter excluded → update indicator.
2. **Dashboard tier** — expensive; shells out to `repodash.py --json` on demand (`fetch_model()`), filters excluded repos from the result, then populates a GTK `ListBox`.

The tray never imports or modifies `repodash.py`. Communication is one-way: subprocess stdout (JSON).

**Config** is persisted to `~/.config/repodash/config.json`. After loading or saving config, `apply_config_to_env(cfg)` must be called so that `detect_terminal()`, `base_dir()`, and the `repodash.py` subprocess all pick up the current values from `os.environ`.

## Key files to understand before editing

| File | Why |
|---|---|
| `tests/fixtures.py` | Builds the shared test repo tree; both Python and Bash tests run against it |
| `repodash.py` lines 566–611 | `Config` + `build_config()` — the canonical config/flag model |
| `tray/repodash_tray.py` lines 41–145 | All module-level config helpers (`CONFIG_DEFAULTS`, `load_config`, `save_config`, `apply_config_to_env`, `resolve_*`) |

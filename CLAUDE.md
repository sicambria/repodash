# CLAUDE.md

## Commands

```bash
# Run the full test suite
python3 -m unittest discover tests -v

# Run tray-only tests (no GTK required)
python3 -m unittest tests/test_tray.py -v

# Parity gate (Python vs Bash JSON output must be byte-identical)
bash tests/test_parity.sh

# Headless tray check (no GUI)
python3 tray/repodash_tray.py --check

# Launch tray (GTK3 + AppIndicator required)
python3 tray/repodash_tray.py
```

## Project layout

```
repodash.py              # Python implementation (canonical)
repodash                 # Bash implementation (parity port)
tray/repodash_tray.py    # GNOME tray icon + dashboard (GTK3, Linux only)
tests/
  fixtures.py            # shared deterministic test repo tree
  test_repodash.py       # Python + parity tests
  test_tray.py           # tray helper tests (no GTK)
  test_parity.sh         # standalone parity gate script
```

## Critical invariants

**Parity contract.** `repodash.py --json` and `repodash --json` must produce semantically identical output for the same inputs. This is enforced by `TestParity` and `tests/test_parity.sh`. If you change the JSON model in one implementation you must change the other too.

**No third-party packages.** `repodash.py` and `tray/repodash_tray.py` are stdlib-only. Never add `pip` dependencies.

**GTK imports inside `run_gui()`.** All `gi`/`Gtk`/`AppIndicator` imports in `tray/repodash_tray.py` live inside `run_gui()` so the module loads cleanly in the test suite without a display.

**Config file.** The tray stores user settings in `~/.config/repodash/config.json` (XDG). `load_config()` merges with `CONFIG_DEFAULTS` and never raises. After loading or saving, call `apply_config_to_env(cfg)` so env-reading helpers (`detect_terminal`, `base_dir`, etc.) and subprocesses (`repodash.py --json`) pick up the values.

## Git workflow

Always commit to local `main` (never to a feature branch). Commit after every change — never leave `main` with uncommitted modifications.

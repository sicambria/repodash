# repodash

A global multi-repo dashboard for developers who work across many git repositories. In one terminal sweep it shows git working-tree status, open `TODO`/`FIXME`/`HACK` markers, audit documents with open checklist items, roadmap open items, and SonarQube stats.

It ships in **two implementations** that are kept at strict parity:

| Implementation | File | Runtime | Best for |
|---|---|---|---|
| **Python** | `repodash.py` | Python 3.8+, **stdlib only** | Linux, macOS, **Windows** — zero install |
| **Bash** | `repodash` | bash 3.2+ | Unix shells; no Python needed |

## The parity invariant — *render is derived; JSON is canonical*

Both implementations build the **same in-memory model** per repo, then either render it to the terminal or serialise it with `--json`. The JSON is the contract: `repodash.py --json` and `repodash --json` produce semantically identical output for the same inputs, enforced by a test gate (`tests/test_parity.sh` and `TestParity`). If you change one implementation, the parity test makes the other's matching change non-optional.

## Install

**Python (any OS, no dependencies):**
```bash
cp repodash.py ~/.local/bin/repodash && chmod +x ~/.local/bin/repodash
# Windows: put repodash.py on PATH, or run `python repodash.py`
```

**Bash:**
```bash
cp repodash ~/.local/bin/repodash && chmod +x ~/.local/bin/repodash
```

`--sonar` in the bash version additionally needs `curl` and `jq`. The Python version needs neither (it uses the standard library `urllib`/`json`).

## Usage

```
repodash [OPTIONS] [DIR]
```

With no arguments it scans `$HOME/git`. Pass a directory to scan a different root.

### Options

| Flag | Effect |
|---|---|
| `--git` `--todos` `--audit` `--roadmap` `--sonar` | show only those sections (combinable) |
| `--dirty` | only show repos with something to report |
| `--json` | emit the full model as JSON (ignores section flags) |
| `--no-color` | disable ANSI color |
| `--insecure` | skip TLS verification for the Sonar request |
| `--depth N` | repo-discovery depth (default 3) |
| `--max-todos N` | max TODO lines shown per repo (default 10) |
| `--width N` | override terminal width (useful for scripting/tests) |
| `-h, --help` | show help |

### Environment

| Variable | Purpose |
|---|---|
| `REPODASH_DIR` | default scan root (overridden by a positional `DIR`) |
| `SONAR_URL` / `SONAR_TOKEN` | SonarQube base URL and auth token |
| `REPODASH_DEPTH` / `REPODASH_MAX_TODOS` | defaults for `--depth` / `--max-todos` |
| `NO_COLOR` | when set (any value), disables color ([no-color.org](https://no-color.org)) |
| `COLUMNS` | terminal width when stdout is not a TTY |

### Examples

```bash
repodash --dirty                 # only repos with something to report
repodash ~/work --git --todos    # git + TODOs for a different root
repodash --json | jq '.repos[] | select(.git.dirty)'   # script against the model
SONAR_URL=http://localhost:9000 SONAR_TOKEN=squ_xxx repodash --sonar --dirty
```

## What each section shows

- **git** — working-tree changes (`git status`), ahead/behind when tracking a remote. A branch both ahead and behind is shown on one line.
- **todo** — `TODO`/`FIXME`/`HACK` in comment context (preceded by `//`, `#`, `<!--`, `--`, or `*`), in source/config files only (generated dirs, `.venv`/`node_modules`, lockfiles and markdown excluded). Up to `--max-todos` shown, with an exact `… and N more`.
- **audit** — `.md`/`.txt` files whose name contains `audit`, `security-review`, `SECURITY`, `vulnerability`, or `pentest`. Open checklist items (`- [ ]` / `* [ ]`, indented sub-tasks included) are listed; dated files (`YYYY-MM-DD…`) are collapsed into an archive count with the most recent surfaced.
- **roadmap** — open checklist items in `ROADMAP.md` / `TODO.md` / `BACKLOG.md`, with line numbers.
- **sonar** — live metrics from the SonarQube API for repos with `sonar-project.properties`. A genuine network failure shows `API unreachable`; an HTTP error surfaces the server's own message (e.g. *Insufficient privileges*) or `HTTP <code>`.

## `--json` schema

Top-level object (stable; `schema_version` bumps only on breaking changes). The **Sonar token never appears anywhere** in this output.

```json
{
  "schema_version": 1,
  "generated_at": "2026-06-23T12:00:00Z",
  "base_dir": "/home/you/git",
  "repos": [
    {
      "path": "/home/you/git/projectA",
      "name": "projectA",
      "git":   { "branch": "main", "ahead": 0, "behind": 0,
                 "dirty": true, "dirty_files": [{"status": "M", "path": "src/x.py"}] },
      "todos": { "total": 14, "shown": 10,
                 "items": [{"path": "src/x.py", "line": 42, "text": "// TODO: ..."}] },
      "audit": { "files": [{"path": "AUDIT.md", "status": "",
                            "items": [{"path": "AUDIT.md", "line": 5, "text": "rotate keys"}]}],
                 "archive": {"count": 3, "most_recent": "2026-05-01-audit.md",
                             "open_items_total": 7, "recent_items": []} },
      "roadmap": { "files": [{"path": "ROADMAP.md",
                              "items": [{"path": "ROADMAP.md", "line": 8, "text": "v2 launch"}]}] },
      "sonar": { "configured": true, "ok": true, "error": null, "project_key": "projectA",
                 "metrics": {"bugs": 0, "vulnerabilities": 1, "coverage": 84.5} }
    }
  ]
}
```

Rules: every section key is always present (empty arrays / `null`, never omitted); Sonar metrics are JSON numbers; `sonar.configured: false` (no properties file) is distinct from `configured: true, ok: false, error: "…"`.

## GNOME tray (Linux)

An optional tray-icon companion lives in [`tray/`](tray/README.md). On Ubuntu 26.04 / GNOME it puts an indicator in the top bar whose menu lists only repos with a dirty working tree — each with one-click **open terminal**, **open Claude Code** (`claude --dangerously-skip-permissions`), **open GitHub**, **open folder**, and **copy path** — plus a searchable/filterable dashboard window of every repo's status. It is a pure consumer of `repodash.py --json` (the cross-platform core is untouched and stays dependency-free); the tray itself needs GTK3 + PyGObject and is Linux-only. See [`tray/README.md`](tray/README.md) for install and autostart.

## Platform notes

- **Windows (Python):** ANSI color is enabled automatically on Windows 10+ consoles via the Console API; on older hosts or when output is redirected, color falls back to plain text. Git-Bash/MSYS (mintty) reports a pipe, so output is plain unless run under a Windows console.
- **macOS HTTPS for Sonar (Python):** the python.org installers ship without a CA bundle until you run *Install Certificates.command*; until then HTTPS Sonar calls fail with a certificate error. Either run that command or use `--insecure` (skips verification — use only against trusted endpoints).
- **Bash portability:** the script targets bash 3.2 (stock macOS) and probes for `grep -P` at runtime, falling back to a POSIX-ERE pattern on BSD/macOS grep. It avoids `mapfile`, negative array indices, and GNU-only `grep -oP`.
- **Submodules:** discovery matches `.git` *directories* only, so submodules and bare repos are not listed as separate repos (their changes still show under the working tree).

## Testing

Stdlib `unittest` (zero dependencies) plus a standalone parity gate:

```bash
python3 -m unittest discover tests        # full suite (auto-skips bash tests if bash absent)
bash tests/test_parity.sh                 # standalone JSON parity gate
```

`tests/fixtures.py` builds a deterministic tree of git repos (including a diverged-branch repo via a bare remote and a mocked Sonar endpoint) that **both** implementations are tested against — this shared fixture is what mechanically prevents the two from drifting.

## Requirements

- **Python version:** Python 3.8+ (no third-party packages).
- **Bash version:** bash 3.2+, git, `grep` (PCRE optional); `curl` + `jq` only for `--sonar`.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). Copyright (C) 2026 repodash contributors.

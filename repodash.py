#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 repodash contributors
"""repodash — multi-repo dashboard: git status, TODOs, audits, roadmap, SonarQube.

Stdlib-only (no third-party packages). Runs on Linux, macOS and Windows with
Python 3.8+.  This is the canonical implementation: it builds a per-repo data
model and then either serialises it (``--json``) or renders it to the terminal.
The bash port (``repodash``) builds the *same* model and must emit byte-identical
``--json`` — that JSON is the parity contract between the two implementations.

Invariant: *render is derived; JSON is canonical.*
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

SCHEMA_VERSION = 1

# ── scan configuration (shared semantics with the bash port) ─────────────────
CODE_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".go", ".rb", ".rs", ".java", ".kt", ".swift", ".dart",
    ".php", ".c", ".cpp", ".h", ".sh", ".bash",
    ".vue", ".svelte", ".sql", ".tf", ".yaml", ".yml", ".toml",
}
MAX_FILE_SIZE = 500 * 1024

# directories pruned everywhere (safe for every section) — speeds up the walk
GLOBAL_PRUNE = {".git", "node_modules"}
# per-section path exclusions (matched against any path component)
TODO_EXCLUDE = {
    ".git", "node_modules", "vendor", ".next", "dist", "build",
    "__pycache__", ".cache", ".claude", "coverage", "test-results",
    "generated", ".venv", "venv", ".tox",
}
AUDIT_EXCLUDE = {
    ".git", "node_modules", "coverage", "dist", "test-results", ".claude",
}
ROADMAP_EXCLUDE = {".git", "node_modules"}

# per-section in-repo depth limits (component count from repo root incl. file)
AUDIT_DEPTH = 5
ROADMAP_DEPTH = 3

TODO_RE = re.compile(r"(?://|#|<!--|--|\*)\s*(TODO|FIXME|HACK)\b")
CHECKBOX_RE = re.compile(r"^\s*[-*] \[ \] (.*)$")
DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
AHEAD_RE = re.compile(r"ahead (\d+)")
BEHIND_RE = re.compile(r"behind (\d+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

ROADMAP_NAMES = {"roadmap.md", "todo.md", "backlog.md"}
SONAR_METRICS = (
    "bugs", "vulnerabilities", "code_smells", "coverage",
    "duplicated_lines_density", "security_hotspots",
)

MAX_TODOS_DEFAULT = 10
MAX_AUDIT_FILES = 5
AUDIT_ITEMS_PER_FILE = 8
ARCHIVE_ITEMS = 5
ROADMAP_ITEMS_PER_FILE = 15


# ── colors ───────────────────────────────────────────────────────────────────
class Palette:
    """ANSI codes, or empty strings when color is disabled."""

    def __init__(self, enabled: bool):
        e = enabled
        self.RED = "\033[0;31m" if e else ""
        self.YELLOW = "\033[1;33m" if e else ""
        self.GREEN = "\033[0;32m" if e else ""
        self.BLUE = "\033[0;34m" if e else ""
        self.CYAN = "\033[0;36m" if e else ""
        self.MAGENTA = "\033[0;35m" if e else ""
        self.BOLD = "\033[1m" if e else ""
        self.DIM = "\033[2m" if e else ""
        self.NC = "\033[0m" if e else ""


def _enable_windows_vt(stream) -> bool:
    """Enable ANSI/VT processing on a Windows console; True if active.

    On non-Windows this is a no-op returning True. The API return value (not a
    Windows-version guess) decides success, so old conhost falls back cleanly.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        # HANDLE is pointer-sized; the default c_int restype sign-truncates it
        # on 64-bit Windows, yielding an invalid handle. Set it explicitly.
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
        kernel32.GetConsoleMode.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)

        std = -12 if stream is sys.stderr else -11
        ENABLE_VT = 0x0004
        handle = kernel32.GetStdHandle(std)
        if not handle or handle == wintypes.HANDLE(-1).value:
            return False
        mode = wintypes.DWORD()
        # Fails (0) when stdout is a pipe/file — that is the redirect detector.
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT))
    except (OSError, AttributeError):
        return False


def want_color(stream, no_color_flag: bool) -> bool:
    """Decide whether to emit ANSI. Precedence: --no-color > NO_COLOR > !tty > win-VT."""
    if no_color_flag or "NO_COLOR" in os.environ:
        return False
    if not getattr(stream, "isatty", lambda: False)():
        return False
    return _enable_windows_vt(stream)


# ── git helpers ──────────────────────────────────────────────────────────────
def _git(repo: str, *args: str) -> str:
    """Run git in *repo*, returning stdout (empty string on any failure)."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def section_git(repo: str) -> dict:
    porcelain = _git(repo, "status", "--porcelain")
    sb = _git(repo, "status", "-sb")
    first = sb.splitlines()[0] if sb else ""

    branch = ""
    if first.startswith("## "):
        rest = first[3:]
        for prefix in ("No commits yet on ", "Initial commit on "):
            if rest.startswith(prefix):
                rest = rest[len(prefix):]
                break
        branch = rest.split("...")[0].split(" ")[0]

    ahead = int(AHEAD_RE.search(first).group(1)) if AHEAD_RE.search(first) else 0
    behind = int(BEHIND_RE.search(first).group(1)) if BEHIND_RE.search(first) else 0

    dirty_files = []
    for line in porcelain.splitlines():
        if not line:
            continue
        dirty_files.append({"status": line[:2], "path": line[3:]})

    return {
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files,
    }


# ── single filesystem walk ───────────────────────────────────────────────────
def collect(repo: str):
    """One walk over *repo*; return (todo_files, audit_files, roadmap_files), each sorted."""
    todo_files, audit_files, roadmap_files = [], [], []
    repo_abs = os.path.abspath(repo)
    base = repo_abs.rstrip(os.sep).count(os.sep)

    for dirpath, dirnames, filenames in os.walk(repo_abs):  # followlinks=False
        # prune always-excluded dirs (slice assignment — reassignment is ignored)
        dirnames[:] = [d for d in dirnames if d not in GLOBAL_PRUNE]
        depth = dirpath.count(os.sep) - base  # 0 at repo root
        rel_parts = () if dirpath == repo_abs else tuple(
            os.path.relpath(dirpath, repo_abs).split(os.sep))

        for fn in filenames:
            full = os.path.join(dirpath, fn)
            low = fn.lower()
            file_depth = depth + 1  # component count incl. the file itself
            parts = set(rel_parts)

            # todos
            if (os.path.splitext(low)[1] in CODE_EXTS
                    and not (parts & TODO_EXCLUDE)):
                try:
                    if os.path.getsize(full) < MAX_FILE_SIZE:
                        todo_files.append(full)
                except OSError:
                    pass

            # audit (name match + .md/.txt, depth <= AUDIT_DEPTH)
            if (low.endswith((".md", ".txt")) and file_depth <= AUDIT_DEPTH
                    and not (parts & AUDIT_EXCLUDE)
                    and ("audit" in low or "security-review" in low
                         or low.startswith("security") or "vulnerability" in low
                         or "pentest" in low)):
                audit_files.append(full)

            # roadmap (exact names, depth <= ROADMAP_DEPTH)
            if (low in ROADMAP_NAMES and file_depth <= ROADMAP_DEPTH
                    and not (parts & ROADMAP_EXCLUDE)):
                roadmap_files.append(full)

    todo_files.sort()
    audit_files.sort()
    roadmap_files.sort()
    return todo_files, audit_files, roadmap_files


def _read_lines(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


# ── todos ────────────────────────────────────────────────────────────────────
def section_todos(repo: str, files, max_todos: int) -> dict:
    items = []
    for path in files:
        rel = os.path.relpath(path, repo)
        for lineno, text in enumerate(_read_lines(path), 1):
            if TODO_RE.search(text):
                items.append({
                    "path": rel.replace(os.sep, "/"),
                    "line": lineno,
                    "text": text.rstrip(),
                })
    return {
        "total": len(items),
        "shown": min(len(items), max_todos),
        "items": items,
    }


def _open_items(path: str, repo: str):
    rel = os.path.relpath(path, repo).replace(os.sep, "/")
    out = []
    for lineno, text in enumerate(_read_lines(path), 1):
        m = CHECKBOX_RE.match(text)
        if m:
            out.append({"path": rel, "line": lineno, "text": m.group(1).rstrip("\r")})
    return out


# ── audit ────────────────────────────────────────────────────────────────────
def section_audit(repo: str, files) -> dict:
    active, archive = [], []
    for f in files:
        (archive if DATE_PREFIX_RE.match(os.path.basename(f)) else active).append(f)

    open_items = []
    for f in active:
        gst = _git(repo, "status", "-s", "--", f).strip()[:2].strip()
        rel = os.path.relpath(f, repo).replace(os.sep, "/")
        items = _open_items(f, repo)
        open_items.append({"path": rel, "status": gst, "items": items})

    archive_block = None
    if archive:
        latest = archive[-1]
        archive_block = {
            "count": len(archive),
            "most_recent": os.path.basename(latest),
            "open_items_total": sum(len(_open_items(f, repo)) for f in archive),
            "recent_items": _open_items(latest, repo),
        }
    return {"files": open_items, "archive": archive_block}


# ── roadmap ──────────────────────────────────────────────────────────────────
def section_roadmap(repo: str, files) -> dict:
    out = []
    for f in files:
        items = _open_items(f, repo)
        if items:
            rel = os.path.relpath(f, repo).replace(os.sep, "/")
            out.append({"path": rel, "items": items})
    return {"files": out}


# ── sonar ────────────────────────────────────────────────────────────────────
def _read_props(path: str):
    key = host = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("sonar.projectKey") and "=" in line:
                    key = line.split("=", 1)[1].strip()
                elif line.startswith("sonar.host.url") and "=" in line:
                    host = line.split("=", 1)[1].strip()
    except OSError:
        pass
    return key, host


def _coerce(value: str):
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return None


def fetch_sonar(url: str, key: str, token: str, insecure: bool, timeout: int = 10):
    import urllib.error
    import urllib.parse
    import urllib.request

    full = (f"{url.rstrip('/')}/api/measures/component"
            f"?component={urllib.parse.quote(key)}"
            f"&metricKeys={','.join(SONAR_METRICS)}")
    headers = {}
    if token:
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"{token}:".encode()).decode()
    req = urllib.request.Request(full, headers=headers)
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:  # subclass of URLError — must come first
        msg = ""
        try:
            payload = json.loads(e.read().decode())
            msg = "; ".join(x.get("msg", "") for x in payload.get("errors", []))
        except Exception:
            pass
        return {"ok": False, "error": msg or f"HTTP {e.code}", "metrics": None}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "error": "timeout", "metrics": None}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"API unreachable ({url})", "metrics": None}
    except OSError as e:
        return {"ok": False, "error": str(e), "metrics": None}

    measures = (data.get("component") or {}).get("measures")
    if measures is None:
        return {"ok": False, "error": "unexpected response", "metrics": None}
    metrics = {}
    for m in measures:
        val = _coerce(m.get("value", ""))
        if val is not None:
            metrics[m["metric"]] = val
    return {"ok": True, "error": None, "metrics": metrics}


def section_sonar(repo: str, cfg) -> dict:
    props = os.path.join(repo, "sonar-project.properties")
    if not os.path.isfile(props):
        return {"configured": False, "ok": None, "error": None,
                "project_key": None, "metrics": None}

    key, host = _read_props(props)
    url = cfg.sonar_url or host or ""
    if not url:
        return {"configured": True, "ok": False,
                "error": "sonar-project.properties found — set SONAR_URL to fetch stats",
                "project_key": key, "metrics": None}
    if not key:
        return {"configured": True, "ok": False,
                "error": "sonar.projectKey missing in sonar-project.properties",
                "project_key": None, "metrics": None}
    res = fetch_sonar(url, key, cfg.sonar_token, cfg.insecure)
    res["configured"] = True
    res["project_key"] = key
    return res


# ── model assembly ───────────────────────────────────────────────────────────
def process_repo(repo: str, cfg) -> dict:
    """Build the full model for one repo. Never raises — errors land in-band."""
    model = {"path": os.path.abspath(repo), "name": os.path.basename(repo.rstrip(os.sep))}
    try:
        todo_files, audit_files, roadmap_files = collect(repo)
    except OSError:
        todo_files = audit_files = roadmap_files = []
    model["git"] = section_git(repo)
    model["todos"] = section_todos(repo, todo_files, cfg.max_todos)
    model["audit"] = section_audit(repo, audit_files)
    model["roadmap"] = section_roadmap(repo, roadmap_files)
    model["sonar"] = section_sonar(repo, cfg)
    return model


def repo_has_content(m: dict, cfg) -> bool:
    if cfg.show_git and m["git"]["dirty"]:
        return True
    if cfg.show_todos and m["todos"]["total"]:
        return True
    if cfg.show_audit and (m["audit"]["files"] or m["audit"]["archive"]):
        return True
    if cfg.show_roadmap and any(f["items"] for f in m["roadmap"]["files"]):
        return True
    if cfg.show_sonar and m["sonar"]["configured"]:
        return True
    return False


def find_repos(base: str, depth: int):
    repos = []
    base = os.path.abspath(base)
    base_level = base.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, _ in os.walk(base):
        level = dirpath.count(os.sep) - base_level
        if os.path.isdir(os.path.join(dirpath, ".git")):
            repos.append(dirpath)
            dirnames[:] = []  # don't descend into a repo's subdirectories
            continue
        if level >= depth:
            dirnames[:] = []
    return sorted(repos)


# ── rendering ────────────────────────────────────────────────────────────────
def _row(p: Palette, label: str, content: str) -> str:
    return f"  {p.BOLD}{p.DIM}{label:<9}{p.NC} {content}"


def render(model: dict, cfg, p: Palette, width: int) -> str:
    name = model["name"]
    lines = []

    # header
    side = max(0, (width - (len(name) + 4) - 2) // 2)
    dash = "─" * side
    lines.append(
        f"\n{p.BOLD}{p.CYAN}╭{dash}{p.NC}{p.BOLD} {name} {p.NC}{p.BOLD}{p.CYAN}{dash}{p.NC}")

    body = []
    if cfg.show_git:
        body.extend(_render_git(model["git"], p))
    if cfg.show_todos:
        body.extend(_render_todos(model["todos"], cfg, p))
    if cfg.show_audit:
        body.extend(_render_audit(model["audit"], p))
    if cfg.show_roadmap:
        body.extend(_render_roadmap(model["roadmap"], p))
    if cfg.show_sonar:
        body.extend(_render_sonar(model["sonar"], p))

    if body:
        lines.extend(body)
    else:
        lines.append(f"  {p.GREEN}✓ clean{p.NC}")

    lines.append(f"{p.BOLD}{p.CYAN}╰{'─' * (width - 1)}{p.NC}")
    return "\n".join(lines)


def _render_git(git: dict, p: Palette):
    if not git["dirty"]:
        return []
    label = "git"
    ab = []
    if git["ahead"]:
        ab.append(f"ahead {git['ahead']}")
    if git["behind"]:
        ab.append(f"behind {git['behind']}")
    if ab:
        label = f"git({' '.join(ab)})"
    out = []
    for i, f in enumerate(git["dirty_files"]):
        out.append(_row(p, label if i == 0 else "", f"{f['status']} {f['path']}"))
    return out


def _render_todos(todos: dict, cfg, p: Palette):
    out = []
    label = "todo"
    for it in todos["items"][:cfg.max_todos]:
        text = f"{it['path']}:{it['line']}:{it['text']}"
        if len(text) > 120:
            text = text[:120] + "…"
        out.append(_row(p, label, f"{p.YELLOW}{text}{p.NC}"))
        label = ""
    if todos["total"] > cfg.max_todos:
        more = todos["total"] - cfg.max_todos
        out.append(_row(p, "", f"{p.DIM}… and {more} more{p.NC}"))
    return out


def _render_audit(audit: dict, p: Palette):
    out = []
    for entry in audit["files"][:MAX_AUDIT_FILES]:
        tag = ""
        if entry["status"] == "??":
            tag = f" {p.DIM}(untracked){p.NC}"
        elif entry["status"] == "M":
            tag = f" {p.DIM}(modified){p.NC}"
        out.append(_row(p, "audit", f"{p.RED}{entry['path']}{p.NC}{tag}"))
        for it in entry["items"][:AUDIT_ITEMS_PER_FILE]:
            out.append(_row(p, "", f"  {p.YELLOW}☐{p.NC} {it['text']}"))
    arch = audit["archive"]
    if arch:
        out.append(_row(p, "audit",
                        f"{p.DIM}{arch['count']} historical files"))
        if arch["open_items_total"] > 0:
            out.append(_row(p, "",
                            f"  {p.YELLOW}{arch['open_items_total']} open items{p.NC}"
                            f" across archive  {p.DIM}(latest: {arch['most_recent']}){p.NC}"))
            for it in arch["recent_items"][:ARCHIVE_ITEMS]:
                out.append(_row(p, "", f"  {p.YELLOW}☐{p.NC} {it['text']}"))
    return out


def _render_roadmap(roadmap: dict, p: Palette):
    out = []
    for entry in roadmap["files"]:
        if not entry["items"]:
            continue
        out.append(_row(p, "roadmap", f"{p.BLUE}{entry['path']}{p.NC}"))
        for it in entry["items"][:ROADMAP_ITEMS_PER_FILE]:
            out.append(_row(p, "", f"  {p.CYAN}☐{p.NC} {it['text']}  {p.DIM}:{it['line']}{p.NC}"))
    return out


def _render_sonar(sonar: dict, p: Palette):
    if not sonar["configured"]:
        return []
    if not sonar["ok"]:
        return [_row(p, "sonar", f"{p.RED}{sonar['error']}{p.NC}")]
    m = sonar["metrics"] or {}

    def g(k):
        return m[k] if k in m else "-"

    def col(k, c):
        v = g(k)
        return c if (v != 0 and v != "-") else p.NC

    bugs, vulns, hot = g("bugs"), g("vulnerabilities"), g("security_hotspots")
    return [_row(p, "sonar",
                 f"bugs:{col('bugs', p.RED)}{bugs}{p.NC}  "
                 f"vulns:{col('vulnerabilities', p.RED)}{vulns}{p.NC}  "
                 f"hotspots:{col('security_hotspots', p.YELLOW)}{hot}{p.NC}  "
                 f"smells:{g('code_smells')}  "
                 f"coverage:{g('coverage')}%  dup:{g('duplicated_lines_density')}%")]


# ── config / CLI ─────────────────────────────────────────────────────────────
class Config:
    pass


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="repodash", add_help=True,
        description="multi-repo dashboard: git, TODOs, audits, roadmap, SonarQube")
    ap.add_argument("dir", nargs="?", help="base directory to scan")
    for s in ("git", "todos", "audit", "roadmap", "sonar"):
        ap.add_argument(f"--{s}", action="store_true", help=f"show only {s} sections")
    ap.add_argument("--dirty", action="store_true", help="only repos with something to report")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="emit the full model as JSON (ignores section flags)")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI color")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification for Sonar")
    ap.add_argument("--depth", type=int, default=int(os.environ.get("REPODASH_DEPTH", "3")),
                    help="repo discovery depth (default 3)")
    ap.add_argument("--max-todos", type=int,
                    default=int(os.environ.get("REPODASH_MAX_TODOS", str(MAX_TODOS_DEFAULT))),
                    help="max TODO lines shown per repo (default 10)")
    ap.add_argument("--width", type=int, default=None, help="override terminal width")
    return ap.parse_args(argv)


def build_config(args):
    cfg = Config()
    cfg.base_dir = args.dir or os.environ.get("REPODASH_DIR") or os.path.join(
        os.path.expanduser("~"), "git")
    section_flags = any([args.git, args.todos, args.audit, args.roadmap, args.sonar])
    if section_flags and not args.as_json:
        cfg.show_git, cfg.show_todos = args.git, args.todos
        cfg.show_audit, cfg.show_roadmap, cfg.show_sonar = args.audit, args.roadmap, args.sonar
    else:
        cfg.show_git = cfg.show_todos = cfg.show_audit = True
        cfg.show_roadmap = cfg.show_sonar = True
    cfg.only_dirty = args.dirty
    cfg.as_json = args.as_json
    cfg.no_color = args.no_color
    cfg.insecure = args.insecure
    cfg.depth = args.depth
    cfg.max_todos = args.max_todos
    cfg.width = args.width
    cfg.sonar_url = os.environ.get("SONAR_URL", "")
    cfg.sonar_token = os.environ.get("SONAR_TOKEN", "")
    return cfg


def resolve_width(cfg):
    if cfg.width:
        return cfg.width
    env = os.environ.get("COLUMNS")
    if env and env.isdigit():
        return int(env)
    return shutil.get_terminal_size((80, 24)).columns


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cfg = build_config(args)

    if not os.path.isdir(cfg.base_dir):
        print(f"Directory not found: {cfg.base_dir}", file=sys.stderr)
        return 1

    repos = find_repos(cfg.base_dir, cfg.depth)
    max_workers = min(len(repos) or 1, min(32, (os.cpu_count() or 1) + 4))
    models = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(process_repo, r, cfg): r for r in repos}
        for fut, repo in futures.items():
            try:
                models[repo] = fut.result()
            except Exception as e:  # defensive: a repo must never abort the run
                models[repo] = {"path": os.path.abspath(repo),
                                "name": os.path.basename(repo), "error": str(e)}
    ordered = [models[r] for r in sorted(models)]

    if cfg.as_json:
        doc = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now_iso(),
            "base_dir": os.path.abspath(cfg.base_dir),
            "repos": ordered,
        }
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0

    if not repos:
        print(f"No git repositories found in: {cfg.base_dir}")
        return 0

    color = want_color(sys.stdout, cfg.no_color)
    p = Palette(color)
    width = resolve_width(cfg)

    word = "repo" if len(repos) == 1 else "repos"
    print(f"{p.BOLD}{p.MAGENTA}repodash{p.NC}  {p.DIM}{cfg.base_dir}{p.NC}  {len(repos)} {word}")

    dirty = 0
    for m in ordered:
        has = repo_has_content(m, cfg)
        if cfg.only_dirty and not has:
            continue
        if has:
            dirty += 1
        print(render(m, cfg, p, width))

    word = "repo" if len(repos) == 1 else "repos"
    print(f"\n{p.BOLD}Summary{p.NC}  {dirty}/{len(repos)} {word} have items")
    return 0


def _now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    sys.exit(main())

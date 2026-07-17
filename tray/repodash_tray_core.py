#!/usr/bin/env python3
# repodash — GNOME tray icon + dashboard window (Linux / GTK3 only).
# Copyright (C) 2026 repodash contributors. GPL-3.0-or-later.
"""A system-tray companion for repodash.

This is a *consumer* of the cross-platform core: it never imports or modifies
``repodash.py`` / ``repodash``. The tray menu does its own cheap ``git status``
probe per repo (so it can refresh often without walking every tree), and only
shells out to ``repodash.py --json`` for the full model when the dashboard
window is opened or refreshed.

Two surfaces:
  * a tray indicator whose menu lists only repos with a dirty working tree, each
    with quick actions (terminal, your configured AI CLI provider, GitHub,
    folder, copy path);
  * a larger dashboard window listing every repo's status with search/filter.

Run ``repodash_tray.py --check`` for a headless dump of what the tray sees
(no GTK required) — useful for verification over SSH.

GTK3 is mandatory because AyatanaAppIndicator3 has no GTK4 binding, and a process
cannot load both GTK3 and GTK4. All ``gi`` imports therefore live inside the GUI
layer so the pure helpers below import cleanly anywhere (and in the test suite).
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional

# ── configuration ────────────────────────────────────────────────────────────
DEFAULT_DEPTH = 3
DEFAULT_INTERVAL = 90  # seconds between cheap menu refreshes
EXPLAIN_TIMEOUT = 300  # seconds for the read-only "Explain changes" run
VERSION = "1.0"
_OPENGODE_GO_FETCH_TIMEOUT = 15
_OPENGODE_GO_HEADER = "━━━ OpenCode Go ━━━"
_FETCHED_OPENGODE_GO_MODELS = None
CLAUDE_COMMAND = "claude --dangerously-skip-permissions"
# Headless "Commit all": the claude binary (resolved via shutil.which) and the
# instruction it runs non-interactively per repo. The run is bounded by a dollar
# budget (--max-budget-usd) rather than turns — this claude build has no
# --max-turns flag, and a budget caps a runaway RCA/fix loop just as well.
CLAUDE_BIN = "claude"
COMMIT_PROMPT = (
    "You are operating non-interactively in a git repository. Follow THIS "
    "repository's own workflow rules (e.g. CLAUDE.md / CONTRIBUTING) strictly.\n"
    "1. Review the working-tree changes (git status, git diff). Group them into "
    "logical commits — one coherent change per commit — and commit them on the "
    "CURRENT branch with messages matching the repo's conventions.\n"
    "2. If a commit fails (failing pre-commit hook, linter, formatter, tests, "
    "etc.), do root-cause analysis, fix the underlying issue by editing files as "
    "needed, and retry. Repeat until it succeeds.\n"
    "3. After everything is committed: if the current branch is NOT the repo's "
    "main branch (main or master), merge the current branch into the main "
    "branch, resolving any conflicts. This merge is a batch policy; if this "
    "repo's own rules explicitly forbid committing/merging to main locally, "
    "SKIP the merge and say so in your result.\n"
    "4. Do NOT push and do not contact any remote.\n"
    "Work autonomously; never ask questions."
)
# Default prompts for the worktree Claude Code actions (shown in Settings → Claude Code).
# {path}, {branch}, {repo_path} are substituted at runtime.
IDLE_CLOSE_PROMPT = (
    "You are operating non-interactively in a git worktree.\n"
    "Worktree: {path}  Branch: {branch}\n"
    "This worktree has no uncommitted changes and has been idle.\n"
    "1. Inspect the branch history and any open todos or roadmap items.\n"
    "2. If the work is complete or no longer needed, remove the worktree:\n"
    "   git worktree remove {path}\n"
    "3. If the branch still needs to be merged or reviewed, report why and do NOT remove it.\n"
    "Work autonomously; never ask questions."
)
STUCK_FINISH_PROMPT = (
    "You are operating non-interactively in a git worktree.\n"
    "Worktree: {path}  Branch: {branch}\n"
    "This worktree has uncommitted changes sitting idle.\n"
    "1. Review all changes (git status, git diff).\n"
    "2. Commit them with appropriate messages, fixing any pre-commit hook failures.\n"
    "3. Merge branch {branch} into local main (unless the repo's own rules forbid it).\n"
    "4. Remove this worktree: git worktree remove {path}\n"
    "Work autonomously; never ask questions."
)
PUSH_PROMPT = (
    "You are operating non-interactively in a git repository. Follow THIS "
    "repository's own workflow rules (e.g. CLAUDE.md / CONTRIBUTING) strictly.\n"
    "1. Run `git push`. If the current branch has no upstream, find the remote "
    "with `git remote` and run `git push -u <remote> HEAD` instead.\n"
    "2. If the push is rejected due to a non-fast-forward divergence, run "
    "`git pull --rebase` and retry the push.\n"
    "3. If the push is rejected by a pre-push hook, diagnose and fix the "
    "underlying issue (run tests, linter, or formatter; edit files as needed), "
    "then retry. Repeat until it succeeds.\n"
    "4. Do NOT commit new changes — only push what is already committed.\n"
    "Work autonomously; never ask questions."
)
EXPLAIN_PROMPT = (
    "You are operating non-interactively in a git repository. This is a "
    "READ-ONLY analysis — do NOT modify, stage, commit, or push anything.\n"
    "1. Inspect any uncommitted changes: `git status`, `git diff`, and "
    "`git diff --staged`.\n"
    "2. Inspect any local commits not yet pushed: `git log @{u}..HEAD` "
    "(or, if the current branch has no upstream, `git log` on the current "
    "branch to see its history).\n"
    "3. Write a concise, plain-English explanation for a developer of what "
    "changed and why it likely matters, as short bullet points. If both "
    "uncommitted changes and unpushed commits are present, cover them in "
    "separate sections; if only one is present, cover just that one.\n"
    "Work autonomously; never ask questions and never ask for confirmation — "
    "just report your findings as your final answer."
)
COMMIT_AND_PUSH_PROMPT = (
    "You are operating non-interactively in a git repository. Follow THIS "
    "repository's own workflow rules (e.g. CLAUDE.md / CONTRIBUTING) strictly.\n"
    "1. Review the working-tree changes (git status, git diff). Group them into "
    "logical commits — one coherent change per commit — and commit them on the "
    "CURRENT branch with messages matching the repo's conventions.\n"
    "2. If a commit fails (failing pre-commit hook, linter, formatter, tests, "
    "etc.), do root-cause analysis, fix the underlying issue by editing files as "
    "needed, and retry. Repeat until it succeeds.\n"
    "3. Run `git push`. If the current branch has no upstream, find the remote "
    "with `git remote` and run `git push -u <remote> HEAD` instead.\n"
    "4. If the push is rejected due to a non-fast-forward divergence, run "
    "`git pull --rebase` and retry the push. If rejected by a pre-push hook, "
    "diagnose and fix the underlying issue, then retry. Repeat until it "
    "succeeds.\n"
    "Work autonomously; never ask questions."
)
# Preference order; first one found on PATH wins unless REPODASH_TERMINAL is set.
TERMINAL_PREFERENCE = ("ptyxis", "gnome-terminal", "kgx", "ghostty", "xterm")

_AHEAD_RE = re.compile(r"ahead (\d+)")
_BEHIND_RE = re.compile(r"behind (\d+)")


def _format_age(age_hours: float) -> str:
    if age_hours < 48:
        return f"{age_hours:.0f}h"
    return f"{age_hours / 24:.1f}d"


CONFIG_DEFAULTS = {
    "base_dir": "",
    "depth": 0,
    "refresh_interval": 0,
    "excluded_repos": [],
    "terminal": "",
    "show_remoteless": True,
    "commit_ram_mb": 2048,      # RAM budget per AI-provider process (MB)
    "commit_max_workers": 0,    # 0 = auto (RAM/CPU derived); >0 = hard cap
    "commit_timeout": 3600,     # seconds per repo (agentic runs are slow)
    "commit_budget_usd": 10.0,  # max $ a single repo's run may spend (claude only)
    "ai_primary_provider": "claude",
    "ai_secondary_provider": "",       # "" = no fallback configured
    "ai_fallback_enabled": True,
    "ai_providers": {
        "claude":   {"model": "sonnet", "effort": "medium"},
        "opencode": {"model": "", "effort": ""},
        "codex":    {"model": "", "effort": "medium"},
        "gemini":   {"model": "", "effort": ""},
    },
    "stale_worktree_idle_hours": 24,
    "stale_worktree_stuck_hours": 12,
    "show_stale_worktrees": True,
    "worktree_idle_close_prompt": "",    # empty → use built-in IDLE_CLOSE_PROMPT
    "worktree_stuck_finish_prompt": "",  # empty → use built-in STUCK_FINISH_PROMPT
}


def base_dir() -> str:
    """Scan root, matching the core: $REPODASH_DIR, else ~/git."""
    return os.environ.get("REPODASH_DIR") or os.path.join(
        os.path.expanduser("~"), "git")


def scan_depth() -> int:
    try:
        return int(os.environ.get("REPODASH_DEPTH", str(DEFAULT_DEPTH)))
    except ValueError:
        return DEFAULT_DEPTH


def refresh_interval() -> int:
    try:
        return max(5, int(os.environ.get("REPODASH_TRAY_INTERVAL",
                                         str(DEFAULT_INTERVAL))))
    except ValueError:
        return DEFAULT_INTERVAL


def config_file() -> str:
    """Path of the per-user config file.

    On Linux / macOS honors $XDG_CONFIG_HOME (default ~/.config).
    On Windows uses %APPDATA%.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(appdata, "repodash", "config.json")
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(config, "repodash", "config.json")


def load_config() -> dict:
    """Load config from disk, merged with defaults. Never raises."""
    import json
    cfg = dict(CONFIG_DEFAULTS)
    cfg["excluded_repos"] = list(CONFIG_DEFAULTS["excluded_repos"])
    cfg["ai_providers"] = {pid: dict(vals) for pid, vals
                          in CONFIG_DEFAULTS["ai_providers"].items()}
    try:
        with open(config_file(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for key in CONFIG_DEFAULTS:
                if key in saved:
                    cfg[key] = saved[key]
            # A saved ai_providers dict may predate a provider id added later
            # (or predate this key entirely) — backfill any missing provider
            # from defaults rather than leaving it absent (KeyError downstream).
            if isinstance(cfg.get("ai_providers"), dict):
                for pid, defaults in CONFIG_DEFAULTS["ai_providers"].items():
                    cfg["ai_providers"].setdefault(pid, dict(defaults))
            else:
                cfg["ai_providers"] = {pid: dict(v) for pid, v
                                       in CONFIG_DEFAULTS["ai_providers"].items()}
            # One-time migration: a config saved before this change may still
            # carry the old flat commit_model/commit_effort keys. Seed the
            # claude provider's settings from them so upgrading doesn't
            # silently reset a user's chosen model/effort.
            if ("commit_model" in saved or "commit_effort" in saved) \
                    and "claude" not in saved.get("ai_providers", {}):
                cfg["ai_providers"]["claude"] = {
                    "model": saved.get("commit_model", "sonnet"),
                    "effort": saved.get("commit_effort", "medium"),
                }
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    """Write config to disk. Never raises."""
    import json
    path = config_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def resolve_base_dir(cfg: dict) -> str:
    return cfg.get("base_dir") or base_dir()


def resolve_depth(cfg: dict) -> int:
    d = cfg.get("depth", 0)
    return int(d) if d and int(d) > 0 else scan_depth()


def resolve_interval(cfg: dict) -> int:
    i = cfg.get("refresh_interval", 0)
    return max(5, int(i)) if i and int(i) > 0 else refresh_interval()


def apply_config_to_env(cfg: dict) -> None:
    """Push config values into os.environ so helpers and subprocesses pick them up.

    fetch_model() shells out to repodash.py --json, which reads REPODASH_DIR /
    REPODASH_DEPTH from the environment. detect_terminal() reads REPODASH_TERMINAL.
    Calling this after load_config() and after every save ensures they stay in sync.
    """
    if cfg.get("base_dir"):
        os.environ["REPODASH_DIR"] = cfg["base_dir"]
    else:
        os.environ.pop("REPODASH_DIR", None)
    depth = cfg.get("depth", 0)
    if depth and int(depth) > 0:
        os.environ["REPODASH_DEPTH"] = str(int(depth))
    else:
        os.environ.pop("REPODASH_DEPTH", None)
    interval = cfg.get("refresh_interval", 0)
    if interval and int(interval) > 0:
        os.environ["REPODASH_TRAY_INTERVAL"] = str(int(interval))
    else:
        os.environ.pop("REPODASH_TRAY_INTERVAL", None)
    terminal = cfg.get("terminal", "").strip()
    if terminal:
        os.environ["REPODASH_TERMINAL"] = terminal
    else:
        os.environ.pop("REPODASH_TERMINAL", None)


# ── git helpers (cheap menu tier) ────────────────────────────────────────────
def _git(repo: str, *args: str) -> str:
    """Run git in *repo*; stdout on success, empty string on any failure.

    Mirrors the defensive style of ``_git`` in repodash.py.
    """
    try:
        out = subprocess.run(["git", "-C", repo, *args],
                             capture_output=True, text=True, timeout=15)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def find_repos(base=None, depth=None):
    """Directories containing a ``.git`` dir, under *base*, to *depth*.

    Same discovery contract as ``find_repos`` in repodash.py: do not descend
    into a repo's own subdirectories, and stop at the depth limit.
    """
    base = os.path.abspath(base if base is not None else base_dir())
    depth = scan_depth() if depth is None else depth
    repos = []
    if not os.path.isdir(base):
        return repos
    base_level = base.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, _ in os.walk(base):
        level = dirpath.count(os.sep) - base_level
        if os.path.isdir(os.path.join(dirpath, ".git")):
            repos.append(dirpath)
            dirnames[:] = []
            continue
        if level >= depth:
            dirnames[:] = []
    return sorted(repos)


def git_status(repo: str) -> dict:
    """Cheap working-tree status for one repo (no filesystem walk).

    Returns branch/ahead/behind/dirty/count without building the full model.
    """
    out = _git(repo, "status", "--porcelain", "-b")
    lines = out.splitlines()
    header = lines[0] if lines and lines[0].startswith("## ") else ""
    files = [ln for ln in lines[1:] if ln.strip()]

    branch = ""
    if header:
        rest = header[3:]
        for prefix in ("No commits yet on ", "Initial commit on "):
            if rest.startswith(prefix):
                rest = rest[len(prefix):]
                break
        branch = rest.split("...")[0].split(" ")[0]

    ahead = int(_AHEAD_RE.search(header).group(1)) if _AHEAD_RE.search(header) else 0
    behind = int(_BEHIND_RE.search(header).group(1)) if _BEHIND_RE.search(header) else 0

    # "Unpushed" = commits on the *current branch* not on any remote. We scope to
    # HEAD (not --branches) so the count matches the branch shown in the menu: a
    # repo with many stale local feature branches would otherwise report their
    # combined commits, inflating the number far past what HEAD is ahead by.
    # rev-list against --remotes (rather than `ahead`) still catches branches that
    # were never pushed — no upstream → ahead 0, but their HEAD commits aren't on
    # any remote. Only meaningful when a remote exists: without one, --not
    # --remotes excludes nothing and the command would count every commit.
    has_remote = bool(_git(repo, "remote").strip())
    unpushed = 0
    if has_remote:
        out = _git(repo, "rev-list", "--count", "HEAD",
                   "--not", "--remotes").strip()
        if out.isdigit():
            unpushed = int(out)

    return {
        "path": os.path.abspath(repo),
        "name": os.path.basename(repo.rstrip(os.sep)),
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "dirty": bool(files),
        "count": len(files),
        "has_remote": has_remote,
        "unpushed": unpushed,
    }


def _parse_worktree_list(raw: str) -> list:
    """Parse 'git worktree list --porcelain' into list of {path, branch} dicts.

    The first entry is the main worktree; callers skip index 0.
    Bare worktrees are excluded.
    """
    entries = []
    current = {}
    for line in raw.splitlines():
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"path": line[9:]}
        elif line.startswith("branch "):
            ref = line[7:]
            prefix = "refs/heads/"
            current["branch"] = ref[len(prefix):] if ref.startswith(prefix) else ref
        elif line == "detached":
            current["branch"] = "(detached)"
        elif line == "bare":
            current["_bare"] = True
    if current:
        entries.append(current)
    return [e for e in entries if not e.get("_bare")]


def scan_worktrees(repo_path: str, idle_hours: float, stuck_hours: float) -> dict:
    """Scan extra worktrees of *repo_path* for stuck/idle/merged states.

    Returns {"stuck": [...], "idle": [...], "merged": [...]}.
    Each entry: {path, branch, last_commit_age_hours, behind, dirty}.

    Stuck:  dirty == True  AND last_commit_age_hours > stuck_hours
    Merged: dirty == False AND age > idle_hours
            AND rev-list HEAD --not <parent> == 0  (no unique commits)
    Idle:   dirty == False AND age > idle_hours
            AND rev-list HEAD --not <parent> > 0  (unique commits not yet in parent)

    Note: "ahead" (relative to remote tracking) is intentionally not used as a
    gate — push state is orthogonal to whether work is unique vs absorbed in the
    parent branch.
    """
    import time
    result: dict = {"stuck": [], "idle": [], "merged": []}
    raw = _git(repo_path, "worktree", "list", "--porcelain")
    if not raw:
        return result
    worktrees = _parse_worktree_list(raw)
    parent_branch = _git(repo_path, "branch", "--show-current").strip() or "main"
    for wt in worktrees[1:]:
        wt_path = wt["path"]
        branch = wt.get("branch", "")
        if not os.path.isdir(wt_path):
            continue
        status_out = _git(wt_path, "status", "--porcelain", "-b")
        if not status_out:
            continue
        lines = status_out.splitlines()
        header = lines[0] if lines and lines[0].startswith("## ") else ""
        files = [ln for ln in lines[1:] if ln.strip()]
        dirty = bool(files)
        ahead = int(_AHEAD_RE.search(header).group(1)) if _AHEAD_RE.search(header) else 0
        behind = int(_BEHIND_RE.search(header).group(1)) if _BEHIND_RE.search(header) else 0
        ct = _git(wt_path, "log", "-1", "--format=%ct").strip()
        if not ct or not ct.isdigit():
            continue
        age_hours = (time.time() - int(ct)) / 3600
        entry = {
            "path": wt_path,
            "branch": branch,
            "last_commit_age_hours": age_hours,
            "behind": behind,
            "dirty": dirty,
        }
        if dirty and age_hours > stuck_hours:
            result["stuck"].append(entry)
        elif not dirty and age_hours > idle_hours:
            unmerged = _git(wt_path, "rev-list", "--count",
                            "HEAD", "--not", parent_branch).strip()
            if unmerged == "0":
                result["merged"].append(entry)
            else:
                result["idle"].append(entry)
    return result


def scan_dirty(base=None, depth=None, cfg=None):
    """All repos' cheap status, sorted by name. Used by the menu and --check."""
    repos = [git_status(r) for r in find_repos(base, depth)]
    if cfg and cfg.get("show_stale_worktrees", True):
        idle_h = cfg.get("stale_worktree_idle_hours", 24)
        stuck_h = cfg.get("stale_worktree_stuck_hours", 12)
        for r in repos:
            r["stale_worktrees"] = scan_worktrees(r["path"], idle_h, stuck_h)
    return repos


# ── GitHub URL resolution ────────────────────────────────────────────────────
_GH_SSH_RE = re.compile(r"^git@github\.com:(?P<path>.+?)(?:\.git)?$")
_GH_SCP_SSH_RE = re.compile(r"^ssh://git@github\.com/(?P<path>.+?)(?:\.git)?$")
_GH_HTTPS_RE = re.compile(r"^https://github\.com/(?P<path>.+?)(?:\.git)?$")


def normalize_github_url(remote: str):
    """Canonical ``https://github.com/owner/repo`` from a remote, else None.

    Handles ``git@github.com:owner/repo.git``, ``ssh://git@github.com/...`` and
    ``https://github.com/owner/repo(.git)``. Non-GitHub remotes return None.
    """
    if not remote:
        return None
    remote = remote.strip()
    for rx in (_GH_SSH_RE, _GH_SCP_SSH_RE, _GH_HTTPS_RE):
        m = rx.match(remote)
        if m:
            return "https://github.com/" + m.group("path").rstrip("/")
    return None


def github_url(repo: str):
    """GitHub web URL for *repo*'s origin remote, or None."""
    return normalize_github_url(_git(repo, "remote", "get-url", "origin").strip())


# ── terminal launching ───────────────────────────────────────────────────────
def detect_terminal():
    """Resolved terminal command, honoring $REPODASH_TERMINAL, else preference.

    Returns the executable name (already confirmed on PATH) or None.
    """
    override = os.environ.get("REPODASH_TERMINAL")
    if override:
        exe = shutil.which(override) or (override if os.path.isabs(override) else None)
        return override if exe else None
    if sys.platform == "win32":
        for term in ("wt", "powershell", "cmd"):
            if shutil.which(term):
                return term
        return None
    for term in TERMINAL_PREFERENCE:
        if shutil.which(term):
            return term
    return None


def _keep_open(command: str) -> str:
    # Run under a login shell, then drop into an interactive shell so the window
    # stays open after the command exits. A login shell is also the right context
    # for `claude` (it warns/refuses in bare non-interactive shells).
    return f"{command}; exec bash"


def terminal_argv(term: str, cwd: str, command=None):
    """argv list to open *term* in *cwd*, optionally running *command*.

    Raises ValueError for an unknown terminal. The basename of *term* selects
    the flag dialect, so absolute paths in $REPODASH_TERMINAL still work.
    """
    name = os.path.basename(term)
    if sys.platform == "win32":
        if name in ("wt", "wt.exe"):
            argv = [term, "-d", cwd]
            if command:
                argv += ["cmd", "/k", command]
            return argv
        if name in ("cmd", "cmd.exe"):
            cmd_line = f'cd /d "{cwd}"'
            if command:
                cmd_line += f" && {command}"
            return [term, "/k", cmd_line]
        if name in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
            ps_cmd = f"Set-Location '{cwd}'"
            if command:
                ps_cmd += f"; {command}"
            return [term, "-NoExit", "-Command", ps_cmd]
        raise ValueError(f"unsupported terminal: {term}")
    if name == "ptyxis":
        argv = [term, "--new-window", "-d", cwd]
        if command:
            argv += ["--", "bash", "-lc", _keep_open(command)]
        return argv
    if name == "gnome-terminal":
        argv = [term, f"--working-directory={cwd}"]
        if command:
            argv += ["--", "bash", "-lc", _keep_open(command)]
        return argv
    if name == "kgx":  # GNOME Console
        argv = [term, "--working-directory", cwd]
        if command:
            argv += ["-e", f"bash -lc '{_keep_open(command)}'"]
        return argv
    if name == "ghostty":
        argv = [term, f"--working-directory={cwd}"]
        if command:
            argv += ["-e", "bash", "-lc", _keep_open(command)]
        return argv
    if name == "xterm":
        argv = [term]
        if command:
            argv += ["-e", "bash", "-lc", _keep_open(command)]
        # xterm has no portable cwd flag; the spawn sets cwd via Popen instead.
        return argv
    raise ValueError(f"unsupported terminal: {term}")


# ── action layer (returns (ok, message); never raises) ───────────────────────
def _spawn(argv, cwd=None, creationflags=0):
    try:
        subprocess.Popen(argv, cwd=cwd,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=creationflags)
        return True, None
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


_NEW_CONSOLE = 0x00000010 if sys.platform == "win32" else 0


def open_terminal(path: str, command=None):
    term = detect_terminal()
    if not term:
        return False, "no terminal found (set REPODASH_TERMINAL)"
    name = os.path.basename(term)
    cf = _NEW_CONSOLE if sys.platform == "win32" and name not in ("wt", "wt.exe") else 0
    return _spawn(terminal_argv(term, path, command), cwd=path, creationflags=cf)


def open_claude(path: str):
    return open_terminal(path, CLAUDE_COMMAND)


def open_url(url: str):
    if not url:
        return False, "no URL"
    if sys.platform == "win32":
        try:
            os.startfile(url)
            return True, None
        except OSError as e:
            return False, str(e)
    return _spawn(["xdg-open", url])



def open_folder(path: str):
    if sys.platform == "win32":
        try:
            os.startfile(os.path.abspath(path))
            return True, None
        except OSError as e:
            return False, str(e)
    return _spawn(["xdg-open", path])


def open_github(path: str):
    url = github_url(path)
    if not url:
        return False, "no GitHub remote"
    return open_url(url)


def clipboard_argv() -> list:
    """argv for the system clipboard tool, chosen by session type / platform."""
    if sys.platform == "win32":
        return ["clip"]
    if sys.platform == "darwin":
        return ["pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY"):
        return ["wl-copy"]
    return ["xclip", "-selection", "clipboard"]


def copy_to_clipboard(text: str):
    """Copy *text* to the system clipboard. Returns (ok, message), never raises.

    Shells out to wl-copy/xclip rather than using Gtk.Clipboard, matching how
    open_folder/open_terminal already delegate to system tools. Gtk.Clipboard
    needs the owning app to keep answering paste requests, which silently does
    nothing when set from a tray/indicator menu item with no focused surface
    (the classic Wayland/XWayland tray-clipboard failure) — set_text()/store()
    return normally either way, so notify() alone can't catch it. A dedicated
    clipboard tool forks and serves the selection independently of our GTK
    event loop, and gives a real exit code to report through notify().
    """
    argv = clipboard_argv()
    tool = argv[0]
    if not shutil.which(tool):
        platform_tools = {"win32": "clip", "darwin": "pbcopy"}
        plat_tool = platform_tools.get(sys.platform, tool)
        return False, f"{tool} not found on PATH (install {plat_tool})"
    try:
        proc = subprocess.run(argv, input=text, text=True,
                              capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if proc.returncode != 0:
        return False, (proc.stderr or f"{tool} exited {proc.returncode}").strip()
    return True, ""


def open_push(path: str):
    # Run `git push` in a terminal rather than silently in the background: a
    # push can prompt for credentials / an ssh passphrase and can fail, and the
    # user must see that. The keep-open shell leaves the result on screen.
    return open_terminal(path, "git push")


def open_commit(path: str):
    # Stage every change first, then open `git commit` so the editor comes up
    # with the working-tree changes ready (the menu's count includes untracked
    # files, which a bare `git commit` would leave unstaged → "nothing to
    # commit"). The keep-open shell leaves the user in the repo afterwards.
    return open_terminal(path, "git add -A && git commit")


def open_wt_claude(wt_path: str, prompt_text: str):
    """Open a terminal running claude headlessly with *prompt_text* in *wt_path*."""
    bin_path = shutil.which(CLAUDE_BIN)
    if not bin_path:
        return False, "claude not found on PATH"
    cmd = f"{bin_path} --dangerously-skip-permissions -p {shlex.quote(prompt_text)}"
    return open_terminal(wt_path, cmd)


def remove_worktree(repo_path: str, wt_path: str, branch: str = "") -> tuple:
    """Remove a git worktree and optionally delete its branch. Returns (ok, message)."""
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, "worktree", "remove", wt_path],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return False, (out.stdout + out.stderr).strip()
        if branch:
            bout = subprocess.run(
                ["git", "-C", repo_path, "branch", "-d", branch],
                capture_output=True, text=True, timeout=15)
            return True, (bout.stdout + bout.stderr).strip() or f"Removed {branch}"
        return True, (out.stdout + out.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


# Environment that disables every interactive auth prompt, so a push that would
# otherwise block on credentials fails fast instead of hanging the progress
# dialog. BatchMode still lets a loaded ssh-agent / credential helper succeed.
_NONINTERACTIVE_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
    "SSH_ASKPASS_REQUIRE": "never",
}


def _current_upstream(path: str, env) -> str:
    """The current branch's upstream ref (e.g. ``origin/main``), or ``""``."""
    try:
        out = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref",
             "--symbolic-full-name", "@{u}"],
            capture_output=True, text=True, timeout=15, env=env)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def push_repo(path: str):
    """Push *path* non-interactively. Returns ``(ok, output)`` and never raises.

    This is the batch counterpart to ``open_push`` (which opens a terminal for
    repos that need an interactive passphrase). When the current branch has an
    upstream it runs a plain ``git push``; otherwise — the never-pushed case the
    unpushed section exists to surface — it runs ``git push -u <remote> HEAD`` so
    the first push actually goes through (and records the upstream).
    """
    env = {**os.environ, **_NONINTERACTIVE_GIT_ENV}
    if _current_upstream(path, env):
        argv = ["git", "-C", path, "push"]
    else:
        remote = next(iter(_git(path, "remote").split()), "")
        if not remote:
            return False, "no remote configured"
        argv = ["git", "-C", path, "push", "-u", remote, "HEAD"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True,
                             timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return False, ("timed out — push needs interactive auth; "
                       "use the repo's 'git push' action in a terminal")
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return out.returncode == 0, (out.stdout + out.stderr).strip()


def _model_effort_args(model: str, effort: str) -> list:
    """--model/--effort flags for a headless claude invocation, if set."""
    args = []
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    return args


def push_claude_argv(bin_path: str, budget_usd: float,
                      model: str = "", effort: str = "") -> list:
    """argv to run claude headlessly with PUSH_PROMPT, bounded by a $ budget."""
    argv = [bin_path, "-p", PUSH_PROMPT,
            "--dangerously-skip-permissions",
            "--output-format", "json"]
    if budget_usd and float(budget_usd) > 0:
        argv += ["--max-budget-usd", str(float(budget_usd))]
    argv += _model_effort_args(model, effort)
    return argv


def commit_stream_argv(bin_path: str, budget_usd: float,
                        model: str = "", effort: str = "") -> list:
    """argv for headless commit that streams live events (stream-json format)."""
    argv = [bin_path, "-p", COMMIT_PROMPT,
            "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--verbose"]
    if budget_usd and float(budget_usd) > 0:
        argv += ["--max-budget-usd", str(float(budget_usd))]
    argv += _model_effort_args(model, effort)
    return argv


def push_claude_stream_argv(bin_path: str, budget_usd: float,
                            model: str = "", effort: str = "") -> list:
    """argv for headless claude-push that streams live events."""
    argv = [bin_path, "-p", PUSH_PROMPT,
            "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--verbose"]
    if budget_usd and float(budget_usd) > 0:
        argv += ["--max-budget-usd", str(float(budget_usd))]
    argv += _model_effort_args(model, effort)
    return argv


def explain_stream_argv(bin_path: str, budget_usd: float,
                        model: str = "", effort: str = "") -> list:
    """argv for headless explain (read-only) that streams live events."""
    argv = [bin_path, "-p", EXPLAIN_PROMPT,
            "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--verbose"]
    if budget_usd and float(budget_usd) > 0:
        argv += ["--max-budget-usd", str(float(budget_usd))]
    argv += _model_effort_args(model, effort)
    return argv


def commit_and_push_stream_argv(bin_path: str, budget_usd: float,
                                model: str = "", effort: str = "") -> list:
    """argv for headless commit-then-push that streams live events."""
    argv = [bin_path, "-p", COMMIT_AND_PUSH_PROMPT,
            "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--verbose"]
    if budget_usd and float(budget_usd) > 0:
        argv += ["--max-budget-usd", str(float(budget_usd))]
    argv += _model_effort_args(model, effort)
    return argv


def explain_actions(r: dict) -> list:
    """Which action buttons the Explain dialog should offer for repo *r*.

    A subset of {"commit", "push", "commit_push"}, based purely on the cheap
    git_status() fields already on hand — no extra probing.
    """
    actions = []
    dirty = bool(r.get("count", 0))
    has_remote = bool(r.get("has_remote"))
    unpushed = bool(has_remote and r.get("unpushed", 0) > 0)
    if dirty:
        actions.append("commit")
    if unpushed:
        actions.append("push")
    if dirty and has_remote:
        actions.append("commit_push")
    return actions


def _fmt_stream_event(line: str) -> str:
    """Parse one stream-json line from an AI provider into human-readable text.

    Returns "" for events that should be silently skipped.
    """
    import json as _json
    try:
        d = _json.loads(line)
    except (ValueError, TypeError):
        return line  # pass non-JSON through verbatim
    t = d.get("type", "")
    if t == "assistant":
        parts = []
        for c in d.get("message", {}).get("content", []):
            if c.get("type") == "text":
                text = c["text"].strip()
                if text:
                    parts.append(text)
            elif c.get("type") == "tool_use":
                name = c.get("name", "?")
                inp = c.get("input", {})
                if isinstance(inp, dict):
                    if "command" in inp:
                        summary = inp["command"][:80]
                    elif "file_path" in inp:
                        summary = inp["file_path"]
                    else:
                        keys = list(inp.keys())
                        summary = f"{keys[0]}=…" if keys else ""
                else:
                    summary = str(inp)[:80]
                parts.append(f"► {name}: {summary}")
        return "\n".join(parts) + "\n" if parts else ""
    if t == "result":
        if d.get("is_error"):
            return f"✗ {d.get('result', 'error')[:120]}\n"
        return f"✓ {d.get('result', 'Done')[:120]}\n"
    return ""


def push_claude_repo(path: str, timeout: int = 900, budget_usd: float = 10.0,
                      model: str = "", effort: str = ""):
    """Run claude in *path* to push via headless Claude. Returns (ok, output). Never raises."""
    bin_path = shutil.which(CLAUDE_BIN)
    if not bin_path:
        return False, "claude not found on PATH"
    try:
        out = subprocess.run(push_claude_argv(bin_path, budget_usd, model, effort), cwd=path,
                             stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    msg = (out.stdout or out.stderr).strip()
    try:
        import json
        doc = json.loads(out.stdout)
        if isinstance(doc, dict) and doc.get("result"):
            msg = str(doc["result"]).strip()
    except ValueError:
        pass
    return out.returncode == 0, msg


# ── batch commit (headless Claude Code) ──────────────────────────────────────
def _mem_available_mb() -> int:
    """Available RAM in MB from /proc/meminfo, or 0 if unknown (non-Linux)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024  # kB → MB
    except (OSError, ValueError):
        pass
    return 0


def commit_workers(ram_mb: int, cap: int) -> int:
    """How many claude processes to run at once.

    MemAvailable // ram_mb, then clamped by CPU count and (if set) the config
    cap. Always at least 1, so the batch still runs on a tiny / unknown host.
    """
    ram_mb = max(256, int(ram_mb or 2048))
    avail = _mem_available_mb()
    n = max(1, avail // ram_mb) if avail > 0 else 1
    n = min(n, os.cpu_count() or 1)
    if cap and int(cap) > 0:
        n = min(n, int(cap))
    return max(1, n)


def commit_argv(bin_path: str, budget_usd: float,
                 model: str = "", effort: str = "") -> list:
    """argv to run claude headlessly with COMMIT_PROMPT, bounded by a $ budget."""
    argv = [bin_path, "-p", COMMIT_PROMPT,
            "--dangerously-skip-permissions",
            "--output-format", "json"]
    if budget_usd and float(budget_usd) > 0:
        argv += ["--max-budget-usd", str(float(budget_usd))]
    argv += _model_effort_args(model, effort)
    return argv


def commit_repo(path: str, timeout: int = 900, budget_usd: float = 10.0,
                 model: str = "", effort: str = ""):
    """Run claude in *path* to commit (and maybe merge). Returns ``(ok, output)``.

    Never raises. ``ok`` is True only on a clean exit; a non-zero exit (error or
    budget exhaustion) may still have landed partial commits — callers re-scan
    afterwards rather than trusting this flag as the final repo state. Unlike
    ``push_repo`` the child keeps the user's full environment (claude needs its
    auth, PATH and node), and stdin is closed so it never blocks on input.
    """
    bin_path = shutil.which(CLAUDE_BIN)
    if not bin_path:
        return False, "claude not found on PATH"
    try:
        out = subprocess.run(commit_argv(bin_path, budget_usd, model, effort), cwd=path,
                             stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    # Prefer claude's JSON "result" summary; fall back to raw output.
    msg = (out.stdout or out.stderr).strip()
    try:
        import json
        doc = json.loads(out.stdout)
        if isinstance(doc, dict) and doc.get("result"):
            msg = str(doc["result"]).strip()
    except ValueError:
        pass
    return out.returncode == 0, msg


# ── multi-provider AI CLI registry ───────────────────────────────────────────
# A "provider" is a real headless-capable agentic CLI. Claude Code is the
# original/default; OpenCode and Codex are fully wired alternates a user can
# pick as primary or configure as a fallback (tried once if the primary is
# missing or a run fails/times out). Gemini CLI is detected and launchable
# interactively, but its headless JSON event schema isn't confirmed stable
# enough yet to wire into the commit/push/explain dialogs (headless=False).
_TASK_PROMPTS = {
    "commit": COMMIT_PROMPT,
    "push": PUSH_PROMPT,
    "commit_and_push": COMMIT_AND_PUSH_PROMPT,
    "explain": EXPLAIN_PROMPT,
}


def resolve_tool_bin(bin_name: str) -> Optional[str]:
    """Find an AI CLI binary — tries PATH first, then common npm/nvm/pip global dirs.
    Single choke point so tests can monkeypatch shutil.which and affect every
    provider consistently."""
    path = shutil.which(bin_name)
    if path:
        return path
    candidates = []
    nvm_bin = os.environ.get("NVM_BIN")
    if nvm_bin:
        candidates.append(os.path.join(nvm_bin, bin_name))
    nvm_root = os.environ.get("NVM_DIR", os.path.expanduser("~/.nvm"))
    nvm_versions = os.path.join(nvm_root, "versions", "node")
    if os.path.isdir(nvm_versions):
        try:
            entries = sorted(os.listdir(nvm_versions), reverse=True)
            for ver in entries:
                candidates.append(os.path.join(nvm_versions, ver, "bin", bin_name))
        except OSError:
            pass
    candidates.extend([
        os.path.join(os.path.expanduser("~"), ".opencode", "bin", bin_name),
        os.path.join(os.path.expanduser("~"), ".npm-global", "bin", bin_name),
        os.path.join(os.path.expanduser("~"), ".npm", "bin", bin_name),
        os.path.join(os.path.expanduser("~"), ".local", "bin", bin_name),
    ])
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _claude_build_argv(bin_path, task, mode, budget_usd, model, effort):
    if task == "commit":
        fn = commit_argv if mode == "json" else commit_stream_argv
    elif task == "push":
        fn = push_claude_argv if mode == "json" else push_claude_stream_argv
    elif task == "commit_and_push":
        fn = commit_and_push_stream_argv
    elif task == "explain":
        fn = explain_stream_argv
    else:
        raise ValueError(f"unknown task: {task}")
    return fn(bin_path, budget_usd, model, effort)


def _opencode_build_argv(bin_path, task, mode, budget_usd, model, effort):
    # No confirmed --max-budget-usd / effort-level equivalent for OpenCode;
    # only --auto (approve-all) and --model are applied.
    argv = [bin_path, "run", _TASK_PROMPTS[task], "--auto", "--format", "json"]
    if model:
        argv += ["--model", model]
    return argv


def _codex_build_argv(bin_path, task, mode, budget_usd, model, effort):
    # No confirmed budget flag for Codex; --dangerously-bypass-approvals-and-sandbox
    # is the closest analog to claude's --dangerously-skip-permissions.
    argv = [bin_path, "exec", _TASK_PROMPTS[task],
            "--dangerously-bypass-approvals-and-sandbox", "--json"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    return argv


def _extract_result_claude(line: str):
    import json as _json
    try:
        d = _json.loads(line)
    except (ValueError, TypeError):
        return None
    if d.get("type") == "result":
        return str(d.get("result", "")).strip()
    return None


def _extract_result_generic(line: str):
    """Best-effort 'final answer' field lookup for any AI provider. Returns
    None if the line isn't a JSON object or has no recognizable result-ish
    field. Providers route to their own extract_result via PROVIDERS, so a
    non-Claude primary uses this function automatically."""
    import json as _json
    try:
        d = _json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    # Providers that use a Claude-like stream-json schema ("type":"result").
    if d.get("type") == "result":
        return str(d.get("result", "")).strip()
    # Flat result fields (text, message, result, response, output, answer).
    for key in ("result", "text", "message", "response", "output", "answer"):
        val = d.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # OpenAI / Anthropic / Gemini content arrays: {"content":[{"type":"text","text":"..."}]}
    content = d.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts).strip()
    # {"message":{"content":[{"type":"text","text":"..."}]}} (OpenAI response wrapper)
    msg = d.get("message")
    if isinstance(msg, dict):
        msg_content = msg.get("content")
        if isinstance(msg_content, list):
            parts = []
            for item in msg_content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "\n".join(parts).strip()
    return None


def _fmt_stream_event_generic(line: str) -> str:
    """Best-effort per-line formatter for providers with an unconfirmed JSON
    schema (opencode, codex). An unrecognized/non-JSON line is echoed back
    verbatim rather than dropped — an unconfirmed schema must degrade to
    plain log lines, never break the run."""
    import json as _json
    try:
        d = _json.loads(line)
    except (ValueError, TypeError):
        return line  # pass non-JSON through verbatim
    if not isinstance(d, dict):
        return line
    result = _extract_result_generic(line)
    if result:
        return f"{result}\n"
    # For JSON objects with no result field, show the object as compact JSON
    # so the user can always see what the provider emitted.
    try:
        return _json.dumps(d, ensure_ascii=False) + "\n"
    except (ValueError, TypeError):
        return line


def _claude_worktree_cmd(bin_path: str, prompt_text: str) -> str:
    return f"{bin_path} --dangerously-skip-permissions -p {shlex.quote(prompt_text)}"


def _opencode_worktree_cmd(bin_path: str, prompt_text: str) -> str:
    return f"{bin_path} run {shlex.quote(prompt_text)} --auto"


def _codex_worktree_cmd(bin_path: str, prompt_text: str) -> str:
    return (f"{bin_path} exec {shlex.quote(prompt_text)} "
            "--dangerously-bypass-approvals-and-sandbox")


@dataclass
class Provider:
    id: str
    label: str
    bin_name: str
    interactive_cmd: str
    headless: bool
    supports_budget: bool
    model_options: list
    effort_options: list
    build_argv: Optional[Callable] = None
    parse_event: Optional[Callable] = None
    extract_result: Optional[Callable] = None
    worktree_cmd: Optional[Callable] = None


PROVIDERS = {
    "claude": Provider(
        id="claude", label="Claude Code", bin_name=CLAUDE_BIN,
        interactive_cmd=CLAUDE_COMMAND,
        headless=True, supports_budget=True,
        model_options=[("fable", "Fable"), ("sonnet", "Sonnet 5"), ("opus", "Opus")],
        effort_options=[("low", "Low"), ("medium", "Medium"), ("high", "High"),
                        ("xhigh", "Extra high"), ("max", "Max")],
        build_argv=_claude_build_argv,
        parse_event=_fmt_stream_event,
        extract_result=_extract_result_claude,
        worktree_cmd=_claude_worktree_cmd,
    ),
    "opencode": Provider(
        id="opencode", label="OpenCode", bin_name="opencode",
        interactive_cmd="opencode",
        headless=True, supports_budget=False,
        model_options=[("opencode/big-pickle", "Big Pickle"),
                      ("opencode/deepseek-v4-flash-free", "DeepSeek V4 Flash Free"),
                      ("opencode/hy3-free", "Hy3 Free"),
                      ("opencode/mimo-v2.5-free", "MiMo V2.5 Free"),
                      ("opencode/nemotron-3-ultra-free", "Nemotron 3 Ultra Free"),
                      ("opencode/north-mini-code-free", "North Mini Code Free")],
        effort_options=[],
        build_argv=_opencode_build_argv,
        parse_event=_fmt_stream_event_generic,
        extract_result=_extract_result_generic,
        worktree_cmd=_opencode_worktree_cmd,
    ),
    "codex": Provider(
        id="codex", label="Codex", bin_name="codex",
        interactive_cmd="codex",
        headless=True, supports_budget=False,
        model_options=[("gpt-5.5", "GPT-5.5"), ("o3", "O3")],
        effort_options=[("low", "Low"), ("medium", "Medium"),
                        ("high", "High"), ("xhigh", "Extra high")],
        build_argv=_codex_build_argv,
        parse_event=_fmt_stream_event_generic,
        extract_result=_extract_result_generic,
        worktree_cmd=_codex_worktree_cmd,
    ),
    "gemini": Provider(
        id="gemini", label="Gemini CLI", bin_name="gemini",
        interactive_cmd="gemini",
        headless=False, supports_budget=False,
        model_options=[("auto", "Auto"), ("pro", "Pro"), ("flash", "Flash")],
        effort_options=[],
    ),
}

HEADLESS_PROVIDER_IDS = [pid for pid, p in PROVIDERS.items() if p.headless]


def _fetch_opencode_go_models():
    global _FETCHED_OPENGODE_GO_MODELS
    bin_path = resolve_tool_bin("opencode")
    if not bin_path:
        print("[repodash] opencode binary not found on PATH", file=sys.stderr)
        _FETCHED_OPENGODE_GO_MODELS = []
        return
    try:
        result = subprocess.run(
            [bin_path, "models", "opencode-go"],
            capture_output=True, text=True, timeout=_OPENGODE_GO_FETCH_TIMEOUT,
        )
        if result.returncode != 0:
            print("[repodash] opencode models failed rc=%d stderr=%s" %
                  (result.returncode, result.stderr.strip()[:200]),
                  file=sys.stderr)
            _FETCHED_OPENGODE_GO_MODELS = []
            return
        models = [line.strip() for line in result.stdout.splitlines()
                  if line.strip()]
        _FETCHED_OPENGODE_GO_MODELS = [(m, m) for m in models]
        print("[repodash] fetched %d models from opencode-go (via %s)" %
              (len(models), bin_path), file=sys.stderr)
    except FileNotFoundError:
        print("[repodash] opencode binary not found on PATH", file=sys.stderr)
        _FETCHED_OPENGODE_GO_MODELS = []
    except subprocess.TimeoutExpired:
        print("[repodash] opencode models timed out", file=sys.stderr)
        _FETCHED_OPENGODE_GO_MODELS = []
    except OSError as e:
        print("[repodash] opencode models OS error: %s" % e, file=sys.stderr)
        _FETCHED_OPENGODE_GO_MODELS = []

def open_provider_terminal(path: str, provider_id: str = "claude"):
    """Open an interactive terminal running *provider_id*'s CLI in *path*."""
    provider = PROVIDERS.get(provider_id) or PROVIDERS["claude"]
    return open_terminal(path, provider.interactive_cmd)


def open_wt_provider(wt_path: str, prompt_text: str, provider_id: str = "claude"):
    """Open a terminal running *provider_id* headlessly with *prompt_text*."""
    provider = PROVIDERS.get(provider_id) or PROVIDERS["claude"]
    if not provider.headless or provider.worktree_cmd is None:
        return False, f"{provider.label} does not support headless worktree actions"
    bin_path = resolve_tool_bin(provider.bin_name)
    if not bin_path:
        return False, f"{provider.bin_name} not found on PATH"
    cmd = provider.worktree_cmd(bin_path, prompt_text)
    return open_terminal(wt_path, cmd)


def _git_op_in_progress(path: str) -> bool:
    """True if *path* has an interrupted git operation (rebase/merge/cherry-pick).

    Handing a repo in this state to a second, different agent is worse than a
    double commit — ``_repo_op_gate`` refuses to fall back when this is true.
    """
    git_dir = os.path.join(path, ".git")
    markers = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD")
    return any(os.path.exists(os.path.join(git_dir, m)) for m in markers)


def _repo_op_gate(path: str, task: str) -> str:
    """After a provider attempt fails, decide whether it's safe to fall back.

    Returns one of:
      "ok_in_effect"    — re-derived state shows the work is already done (the
                          failing exit code was misleading); no fallback.
      "needs_attention" — an interrupted git operation was left behind; never
                          hand this to a second, different agent.
      "retry"           — work genuinely remains; safe to try the next provider.

    Every prompt (COMMIT_PROMPT/PUSH_PROMPT/...) inspects current git state
    before acting, so "retry" always means "do the remaining work", never
    "replay a finished job" — this is what makes one extra hop safe.
    """
    if _git_op_in_progress(path):
        return "needs_attention"
    if not _git(path, "rev-parse", "--git-dir").strip():
        return "needs_attention"
    status = git_status(path)
    if task in ("commit", "commit_and_push") and status["dirty"]:
        return "retry"
    if (task in ("push", "commit_and_push") and status["has_remote"]
            and status["unpushed"] > 0):
        return "retry"
    if task in ("commit", "push", "commit_and_push"):
        return "ok_in_effect"
    return "retry"  # unknown/other task: default to giving the fallback a shot


def provider_selection(cfg: dict) -> dict:
    """Primary/secondary provider ids + fallback flag + resolved per-provider
    model/effort, derived from *cfg*. Shared by every commit/push/explain
    dialog so they all read the same primary/secondary/fallback settings."""
    providers_cfg = cfg.get("ai_providers", {})
    primary = cfg.get("ai_primary_provider", "claude")
    secondary = cfg.get("ai_secondary_provider", "")
    return {
        "primary": primary,
        "secondary": secondary if secondary and secondary != primary else "",
        "fallback_enabled": bool(cfg.get("ai_fallback_enabled", True)),
        "models": {pid: providers_cfg.get(pid, {}).get("model", "")
                  for pid in PROVIDERS},
        "efforts": {pid: providers_cfg.get(pid, {}).get("effort", "")
                   for pid in PROVIDERS},
    }


# ── autostart (configurable from the menu) ───────────────────────────────────
_AUTOSTART_NAME = "repodash-tray.desktop"
_AUTOSTART_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_REG_VALUE = "repodash-tray"


def autostart_file() -> str:
    """Path of the per-user autostart entry.

    On Linux / macOS returns the .desktop file path (honors $XDG_CONFIG_HOME).
    On Windows returns the registry key path string.
    """
    if sys.platform == "win32":
        return f"HKCU\\{_AUTOSTART_REG_KEY}\\{_AUTOSTART_REG_VALUE}"
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(config, "autostart", _AUTOSTART_NAME)


def autostart_enabled() -> bool:
    if sys.platform == "win32":
        try:
            import winreg
        except ImportError:
            return False
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 _AUTOSTART_REG_KEY,
                                 0, winreg.KEY_READ)
            try:
                winreg.QueryValueEx(key, _AUTOSTART_REG_VALUE)
                return True
            except FileNotFoundError:
                return False
            finally:
                winreg.CloseKey(key)
        except OSError:
            return False
    return os.path.isfile(autostart_file())


def _autostart_contents() -> str:
    if sys.platform == "win32":
        exe = sys.executable or "python"
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "repodash_tray_win.py")
        return f'"{exe}" "{script}"'
    # Absolute interpreter + script path: autostart runs with a minimal PATH.
    exe = sys.executable or "/usr/bin/python3"
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "repodash_tray.py")
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=repodash tray\n"
        "Comment=Tray icon for the repodash multi-repo dashboard\n"
        f"Exec={exe} {script}\n"
        "Icon=utilities-terminal\n"
        "Terminal=false\n"
        "Categories=Development;Utility;\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=3\n"
    )


def set_autostart(enabled: bool) -> bool:
    """Enable/disable login autostart.

    On Linux / macOS writes/removes a .desktop entry.
    On Windows writes/removes a registry Run key.
    """
    if sys.platform == "win32":
        try:
            import winreg
        except ImportError:
            return False
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 _AUTOSTART_REG_KEY,
                                 0, winreg.KEY_SET_VALUE | winreg.KEY_READ)
            try:
                if enabled:
                    winreg.SetValueEx(key, _AUTOSTART_REG_VALUE, 0,
                                      winreg.REG_SZ, _autostart_contents())
                else:
                    try:
                        winreg.DeleteValue(key, _AUTOSTART_REG_VALUE)
                    except FileNotFoundError:
                        pass
            finally:
                winreg.CloseKey(key)
        except OSError:
            pass
        return autostart_enabled()
    path = autostart_file()
    if enabled:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(_autostart_contents())
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    return autostart_enabled()


# ── full model (dashboard tier) ──────────────────────────────────────────────
def _core_script() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        os.pardir, "repodash.py")


def fetch_model() -> dict:
    """Run ``repodash.py --json`` and parse it. Never raises.

    On any failure returns ``{"error": "...", "repos": []}`` so callers can
    render the problem instead of crashing.
    """
    import json
    script = _core_script()
    if not os.path.isfile(script):
        return {"error": f"core not found: {script}", "repos": []}
    try:
        out = subprocess.run([sys.executable, script, "--json"],
                             capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e), "repos": []}
    if out.returncode != 0:
        return {"error": out.stderr.strip() or f"exit {out.returncode}",
                "repos": []}
    try:
        return json.loads(out.stdout)
    except ValueError as e:
        return {"error": f"bad JSON from core: {e}", "repos": []}


# ── headless self-check ──────────────────────────────────────────────────────
def run_check() -> int:
    """Print what the tray sees, without starting GTK. Returns an exit code."""
    cfg = load_config()
    cfile = config_file()
    cfg_status = "found" if os.path.isfile(cfile) else "not found (using defaults)"
    print(f"config    : {cfile}  [{cfg_status}]")

    base = resolve_base_dir(cfg)
    depth = resolve_depth(cfg)
    interval = resolve_interval(cfg)
    excluded = set(cfg.get("excluded_repos", []))
    show_remoteless = cfg.get("show_remoteless", True)
    print(f"scan root : {base}  (depth {depth}, interval {interval}s)")
    print(f"remoteless: {'shown' if show_remoteless else 'hidden'} in menu")
    if excluded:
        print(f"excluded  : {len(excluded)} repo(s)")
        for p in sorted(excluded):
            print(f"  - {p}")

    term = detect_terminal()
    print(f"terminal  : {term or '(none found — set REPODASH_TERMINAL)'}")
    if term:
        print("  terminal argv  :", terminal_argv(term, base))
        print("  claude argv    :", terminal_argv(term, base, CLAUDE_COMMAND))
    print(f"autostart : {'on' if autostart_enabled() else 'off'}  "
          f"({autostart_file()})")

    ram_mb = cfg.get("commit_ram_mb", 2048)
    cap = cfg.get("commit_max_workers", 0)
    workers = commit_workers(ram_mb, cap)
    cap_desc = f"cap {cap}" if cap else "auto"
    avail = _mem_available_mb()
    avail_desc = f"{avail} MB avail" if avail else "RAM unknown"
    print(f"commit    : {ram_mb} MB/proc, {cap_desc} → {workers} workers, "
          f"{cfg.get('commit_timeout', 3600)}s timeout, "
          f"${cfg.get('commit_budget_usd', 10.0)}/repo (claude only)  [{avail_desc}]")

    sel = provider_selection(cfg)
    print(f"AI primary: {PROVIDERS.get(sel['primary'], PROVIDERS['claude']).label} "
          f"({sel['primary']})")
    print(f"AI fallback: "
          f"{PROVIDERS[sel['secondary']].label if sel['secondary'] else '(none)'}"
          f"  [{'enabled' if sel['fallback_enabled'] else 'disabled'}]")
    for pid, provider in PROVIDERS.items():
        bin_path = resolve_tool_bin(provider.bin_name) or "(not on PATH)"
        detail = (f"model {sel['models'].get(pid) or '(default)'}"
                 + (f", effort {sel['efforts'][pid]}" if sel["efforts"].get(pid) else ""))
        headless_note = "" if provider.headless else "  [interactive-only]"
        print(f"  {provider.label:<12}: {bin_path}  ({detail}){headless_note}")

    repos = scan_dirty(base, depth, cfg)
    repos = [r for r in repos if r["path"] not in excluded]
    if not show_remoteless:
        repos = [r for r in repos if r["has_remote"]]
    dirty = [r for r in repos if r["dirty"]]
    unpushed = [r for r in repos if r["has_remote"] and r["unpushed"] > 0]
    print(f"\nrepos     : {len(repos)} found, {len(dirty)} dirty, "
          f"{len(unpushed)} unpushed")
    for r in dirty:
        track = ""
        if r["ahead"] or r["behind"]:
            track = f"  ▲{r['ahead']} ▼{r['behind']}"
        gh = github_url(r["path"])
        print(f"  • {r['name']}  [{r['branch']}{track}]  "
              f"{r['count']} file(s)" + (f"  {gh}" if gh else ""))
    if unpushed:
        print("\nunpushed  :")
        for r in unpushed:
            gh = github_url(r["path"])
            print(f"  • {r['name']}  [{r['branch']} +{r['unpushed']}]"
                  + (f"  {gh}" if gh else ""))
    stuck_all = [(r, w) for r in repos
                 for w in r.get("stale_worktrees", {}).get("stuck", [])]
    idle_all = [(r, w) for r in repos
                for w in r.get("stale_worktrees", {}).get("idle", [])]
    if stuck_all:
        print("\nstuck wt  :")
        for r, w in stuck_all:
            print(f"  ⚠ {r['name']}  [{w['branch']}]  "
                  f"{_format_age(w['last_commit_age_hours'])} ago  dirty")
    if idle_all:
        print("\nidle wt   :")
        for r, w in idle_all:
            behind_s = f"  ▼{w['behind']}" if w["behind"] else ""
            print(f"  ⏸ {r['name']}  [{w['branch']}]  "
                  f"{_format_age(w['last_commit_age_hours'])} ago{behind_s}")
    merged_all = [(r, w) for r in repos
                  for w in r.get("stale_worktrees", {}).get("merged", [])]
    if merged_all:
        print("\nmerged wt :")
        for r, w in merged_all:
            print(f"  ✓ {r['name']}  [{w['branch']}]  "
                  f"{_format_age(w['last_commit_age_hours'])} ago  (absorbed in main — safe to remove)")
    return 0



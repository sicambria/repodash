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
    """Path of the per-user config file (honors $XDG_CONFIG_HOME)."""
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
    depth = cfg.get("depth", 0)
    if depth and int(depth) > 0:
        os.environ["REPODASH_DEPTH"] = str(int(depth))
    interval = cfg.get("refresh_interval", 0)
    if interval and int(interval) > 0:
        os.environ["REPODASH_TRAY_INTERVAL"] = str(int(interval))
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
def _spawn(argv, cwd=None):
    try:
        subprocess.Popen(argv, cwd=cwd,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True, None
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def open_terminal(path: str, command=None):
    term = detect_terminal()
    if not term:
        return False, "no terminal found (set REPODASH_TERMINAL)"
    # xterm needs cwd from Popen; others embed it in argv. Passing cwd is
    # harmless for the rest, so always pass it.
    return _spawn(terminal_argv(term, path, command), cwd=path)


def open_claude(path: str):
    return open_terminal(path, CLAUDE_COMMAND)


def open_url(url: str):
    if not url:
        return False, "no URL"
    return _spawn(["xdg-open", url])


def open_github(path: str):
    url = github_url(path)
    if not url:
        return False, "no GitHub remote"
    return open_url(url)


def open_folder(path: str):
    return _spawn(["xdg-open", path])


def clipboard_argv() -> list:
    """argv for the system clipboard tool, chosen by session type."""
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
        fallback = "xclip" if tool == "wl-copy" else "wl-copy"
        return False, f"{tool} not found on PATH (install {tool}, or {fallback})"
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
    """argv for headless claude-explain (read-only) that streams live events."""
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
    """Parse one stream-json line from claude into human-readable text.

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
    """Find an AI CLI binary — tries PATH first, then common npm global dirs.
    Single choke point so tests can monkeypatch shutil.which and affect every
    provider consistently."""
    path = shutil.which(bin_name)
    if path:
        return path
    candidates = []
    nvm_bin = os.environ.get("NVM_BIN")
    if nvm_bin:
        candidates.append(os.path.join(nvm_bin, bin_name))
    candidates.extend([
        os.path.join(os.path.expanduser("~"), ".opencode", "bin", bin_name),
        os.path.join(os.path.expanduser("~"), ".npm-global", "bin", bin_name),
        os.path.join(os.path.expanduser("~"), ".npm", "bin", bin_name),
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
    """Best-effort 'final answer' field lookup for providers whose JSON event
    schema isn't confirmed. Returns None if the line isn't a JSON object or
    has no recognizable result-ish field."""
    import json as _json
    try:
        d = _json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    for key in ("result", "text", "message"):
        val = d.get(key)
        if val:
            return str(val).strip()
    return None


def _fmt_stream_event_generic(line: str) -> str:
    """Best-effort per-line formatter for providers with an unconfirmed JSON
    schema (opencode, codex). An unrecognized/non-JSON line is echoed back
    verbatim rather than dropped — an unconfirmed schema must degrade to
    plain log lines, never break the run."""
    import json as _json
    try:
        _json.loads(line)
    except (ValueError, TypeError):
        return line  # pass non-JSON through verbatim
    result = _extract_result_generic(line)
    return f"{result}\n" if result else ""


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
    try:
        result = subprocess.run(
            ["opencode", "models", "opencode-go"],
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
        print("[repodash] fetched %d models from opencode-go" % len(models),
              file=sys.stderr)
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


def autostart_file() -> str:
    """Path of the per-user autostart entry (honors $XDG_CONFIG_HOME)."""
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(config, "autostart", _AUTOSTART_NAME)


def autostart_enabled() -> bool:
    return os.path.isfile(autostart_file())


def _autostart_contents() -> str:
    # Absolute interpreter + script path: autostart runs with a minimal PATH.
    exe = sys.executable or "/usr/bin/python3"
    script = os.path.abspath(__file__)
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
    """Enable/disable login autostart by writing/removing the .desktop entry."""
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


# ── GUI layer (GTK3) ─────────────────────────────────────────────────────────
def run_gui() -> int:
    import gi
    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        try:
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3 as AppIndicator
        except (ValueError, ImportError):
            sys.stderr.write(
                "error: no AppIndicator typelib found. Install "
                "gir1.2-ayatanaappindicator3-0.1 (see tray/README.md).\n")
            return 1
    from gi.repository import Gtk, GLib, Gdk
    import threading

    def _screen_fraction_size(parent, w_frac=0.7, h_frac=0.7):
        """(width, height) at *w_frac*/*h_frac* of the parent's monitor.

        Falls back to the default display's primary monitor when *parent*
        has no realized window (e.g. the tray has no dashboard open).
        Clamped to a sane minimum so a tiny/unknown monitor never produces
        an unusably small dialog.
        """
        display = Gdk.Display.get_default()
        monitor = None
        window = parent.get_window() if parent is not None else None
        if display is not None and window is not None:
            monitor = display.get_monitor_at_window(window)
        if monitor is None and display is not None:
            monitor = display.get_monitor(0)
        if monitor is None:
            return 480, 420
        geo = monitor.get_geometry()
        return max(400, int(geo.width * w_frac)), max(300, int(geo.height * h_frac))

    APP_ID = "org.repodash.Tray"
    ICON_SVG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "repodash.svg")
    FALLBACK_ICON = "utilities-terminal"

    def warn_if_no_indicator_extension():
        if not shutil.which("gnome-extensions"):
            return
        try:
            out = subprocess.run(["gnome-extensions", "list", "--enabled"],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return
        if "appindicator" not in out.stdout.lower():
            sys.stderr.write(
                "warning: no AppIndicator GNOME extension enabled — the tray "
                "icon may not appear. Enable 'Ubuntu AppIndicators'.\n")

    def notify(parent, ok, message):
        """Surface an action failure; success is silent."""
        if ok:
            return
        dlg = Gtk.MessageDialog(transient_for=parent, modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.OK,
                                text="Action failed")
        dlg.format_secondary_text(message or "unknown error")
        dlg.run()
        dlg.destroy()

    class TrayApp(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID)
            self.indicator = None
            self.window = None
            self.repos = []          # cheap status list (menu tier)
            self.model = None        # full model (dashboard tier), lazy
            self._timer_id = 0
            self._op_running = False  # True while a commit/push dialog is open
            self.config = load_config()
            apply_config_to_env(self.config)

        def quit(self):
            """Force-quit even when a modal dialog (commit, push, settings, …)
            is open.  Destroy all windows so that any blocking Gtk.Dialog.run()
            call returns immediately, then hand off to Gtk.Application.quit()."""
            for w in list(self.get_windows()):
                w.destroy()
            Gtk.Application.quit(self)

        # -- lifecycle --
        def do_startup(self):
            Gtk.Application.do_startup(self)
            self.hold()  # stay alive without a window (tray-resident)
            self._first_activate = True
            warn_if_no_indicator_extension()
            # libappindicator resolves icons by *name* within a theme path more
            # reliably than by absolute file path; register tray/ as a theme dir
            # and reference "repodash" (→ repodash.svg). Fall back to a stock
            # theme icon if our SVG is missing.
            have_icon = os.path.isfile(ICON_SVG)
            icon_name = "repodash" if have_icon else FALLBACK_ICON
            self.indicator = AppIndicator.Indicator.new(
                "repodash-tray", icon_name,
                AppIndicator.IndicatorCategory.APPLICATION_STATUS)
            if have_icon:
                self.indicator.set_icon_theme_path(
                    os.path.dirname(os.path.abspath(__file__)))
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_title("repodash")
            self.indicator.set_menu(self._build_menu())
            self.refresh_menu()
            threading.Thread(target=_fetch_opencode_go_models, daemon=True).start()
            self._timer_id = GLib.timeout_add_seconds(
                resolve_interval(self.config), self._on_timer)

        def do_activate(self):
            # GtkApplication fires `activate` on the primary instance at normal
            # startup too — stay quietly tray-resident on first launch, and only
            # surface the dashboard on a genuine re-activation (second launch).
            if self._first_activate:
                self._first_activate = False
                return
            self.show_dashboard()

        def _on_timer(self):
            self.refresh_menu()
            return True  # keep ticking

        # -- menu tier --
        def refresh_menu(self):
            cfg = self.config
            self.indicator.set_label("↻", "")

            def work():
                base = resolve_base_dir(cfg)
                depth = resolve_depth(cfg)
                excluded = set(cfg.get("excluded_repos", []))
                repos = scan_dirty(base, depth, cfg)
                repos = [r for r in repos if r["path"] not in excluded]
                if not cfg.get("show_remoteless", True):
                    repos = [r for r in repos if r["has_remote"]]
                # Resolve GitHub URLs off the main thread so menu-building
                # (which runs on the GTK thread) never blocks on git. Both the
                # dirty and unpushed sections expose an "Open GitHub" action.
                for r in repos:
                    sw = r.get("stale_worktrees") or {}
                    if (r["dirty"] or (r["has_remote"] and r["unpushed"])
                            or sw.get("stuck") or sw.get("idle") or sw.get("merged")):
                        r["github"] = github_url(r["path"])
                GLib.idle_add(self._apply_repos, repos)
            threading.Thread(target=work, daemon=True).start()

        def _apply_repos(self, repos):
            self.repos = repos
            self.indicator.set_menu(self._build_menu())
            dirty = sum(1 for r in repos if r["dirty"])
            self.indicator.set_label(str(dirty) if dirty else "", "")
            return False

        def _build_menu(self):
            menu = Gtk.Menu()
            dirty = [r for r in self.repos if r["dirty"]]
            unpushed = [r for r in self.repos
                        if r["has_remote"] and r["unpushed"] > 0]
            header = Gtk.MenuItem(
                label=f"{len(dirty)} dirty · {len(unpushed)} unpushed · "
                      f"{len(self.repos)} repos")
            header.set_sensitive(False)
            menu.append(header)
            menu.append(Gtk.SeparatorMenuItem())

            for r in dirty:
                menu.append(self._repo_item(r))
            if not dirty:
                clean = Gtk.MenuItem(label="✓ all clean")
                clean.set_sensitive(False)
                menu.append(clean)
            else:
                self._action(menu, f"Commit all via {self._ai_label()} ({len(dirty)})…",
                             lambda *_: self._on_commit_all())

            # Unpushed repos get their own section (a repo can be both dirty and
            # unpushed — it then appears in both lists, each complete on its own).
            if unpushed:
                menu.append(Gtk.SeparatorMenuItem())
                sub_header = Gtk.MenuItem(label="Unpushed")
                sub_header.set_sensitive(False)
                menu.append(sub_header)
                for r in unpushed:
                    menu.append(self._repo_item(r, unpushed=True))
                self._action(menu, f"Push all ({len(unpushed)})…",
                             lambda *_: self._on_push_all())
                self._action(menu, f"Push all via {self._ai_label()} ({len(unpushed)})…",
                             lambda *_: self._on_push_claude_all())

            stuck_repos = [r for r in self.repos
                           if r.get("stale_worktrees", {}).get("stuck")]
            idle_repos = [r for r in self.repos
                          if r.get("stale_worktrees", {}).get("idle")]
            merged_repos = [r for r in self.repos
                            if r.get("stale_worktrees", {}).get("merged")]

            if stuck_repos:
                menu.append(Gtk.SeparatorMenuItem())
                hdr = Gtk.MenuItem(label="⚠ Stuck worktrees")
                hdr.set_sensitive(False)
                menu.append(hdr)
                for r in stuck_repos:
                    menu.append(self._stale_repo_item(r, "stuck"))

            if idle_repos:
                menu.append(Gtk.SeparatorMenuItem())
                hdr = Gtk.MenuItem(label="⏸ Idle worktrees")
                hdr.set_sensitive(False)
                menu.append(hdr)
                for r in idle_repos:
                    menu.append(self._stale_repo_item(r, "idle"))

            if merged_repos:
                menu.append(Gtk.SeparatorMenuItem())
                hdr = Gtk.MenuItem(label="✓ Merged worktrees")
                hdr.set_sensitive(False)
                menu.append(hdr)
                for r in merged_repos:
                    menu.append(self._stale_repo_item(r, "merged"))

            menu.append(Gtk.SeparatorMenuItem())
            self._action(menu, "Show dashboard…",
                         lambda *_: self.show_dashboard())
            self._action(menu, "Refresh now", lambda *_: self.refresh_menu())
            self._action(menu, "Settings…", lambda *_: self._on_settings())
            self._action(menu, "Help…", lambda *_: self._on_help())
            self._action(menu, "About…", lambda *_: self._on_about())

            start_item = Gtk.CheckMenuItem(label="Start on login")
            start_item.set_active(autostart_enabled())  # set before connecting
            start_item.connect("toggled", self._on_toggle_autostart)
            menu.append(start_item)

            menu.append(Gtk.SeparatorMenuItem())
            self._action(menu, "Quit", lambda *_: self.quit())
            menu.show_all()
            return menu

        def _on_toggle_autostart(self, item):
            ok = set_autostart(item.get_active())
            # Reflect the real on-disk result (e.g. if the write failed).
            if ok != item.get_active():
                item.set_active(ok)

        def _show_error(self, title, detail):
            """Surface a dialog-construction failure (does not use a
            potentially-broken ConfigDialog).  Returns False if even the
            error dialog could not be shown (bare terminal fallback)."""
            try:
                parent = self.window if (self.window and self.window.get_visible()) else None
                dlg = Gtk.MessageDialog(transient_for=parent, modal=True,
                                        message_type=Gtk.MessageType.ERROR,
                                        buttons=Gtk.ButtonsType.OK,
                                        text=title)
                dlg.format_secondary_text(detail)
                dlg.run()
                dlg.destroy()
                return True
            except Exception:
                import traceback
                traceback.print_exc()
                return False

        def _on_settings(self):
            parent = self.window if (self.window and self.window.get_visible()) else None
            try:
                dlg = ConfigDialog(parent, self.config)
                response = dlg.run()
                if response == Gtk.ResponseType.OK:
                    self.config = dlg.get_config()
                    apply_config_to_env(self.config)
                    save_config(self.config)
                    if self._timer_id:
                        GLib.source_remove(self._timer_id)
                    self._timer_id = GLib.timeout_add_seconds(
                        resolve_interval(self.config), self._on_timer)
                    self.refresh_menu()
                    if self.window is not None:
                        self.window.set_config(self.config)
                        if self.window.get_visible():
                            self.window.reload()
                dlg.destroy()
            except Exception:
                import traceback
                msg = traceback.format_exc()
                traceback.print_exc()
                self._show_error(
                    "Settings could not be opened",
                    "A bug in the settings dialog construction prevented it "
                    "from being shown. The error has been printed to the "
                    "terminal.\n\n" + msg.split("\n")[-2])

        def _run_op_dialog(self, dlg):
            """Run a commit/push dialog, blocking concurrent ops."""
            if self._op_running:
                return
            self._op_running = True
            try:
                dlg.run()
            finally:
                self._op_running = False
                dlg.destroy()
                self.refresh_menu()

        def _on_push_all(self):
            if self._op_running:
                return
            # Recompute from the latest scan (the menu may have refreshed since
            # it was built) so we never push a stale set.
            repos = [r for r in self.repos
                     if r["has_remote"] and r["unpushed"] > 0]
            if not repos:
                return
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(PushAllDialog(parent, repos))

        def _on_commit_all(self):
            if self._op_running:
                return
            # Recompute the dirty set from the latest scan so a refresh between
            # menu-build and click never commits a stale list.
            repos = [r for r in self.repos if r["dirty"]]
            if not repos:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(CommitAllDialog(parent, repos,
                                                cfg.get("commit_ram_mb", 2048),
                                                cfg.get("commit_max_workers", 0),
                                                cfg.get("commit_timeout", 3600),
                                                cfg.get("commit_budget_usd", 10.0),
                                                provider_selection(cfg), "commit"))

        def _on_commit_repo(self, r):
            if self._op_running:
                return
            # Single-repo counterpart to _on_commit_all: same headless AI
            # flow (logical chunks, repo-conventional messages, docs, merge),
            # just scoped to one repo via the shared progress dialog.
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(CommitAllDialog(parent, [r],
                                                cfg.get("commit_ram_mb", 2048),
                                                cfg.get("commit_max_workers", 0),
                                                cfg.get("commit_timeout", 3600),
                                                cfg.get("commit_budget_usd", 10.0),
                                                provider_selection(cfg), "commit"))

        def _on_push_claude_repo(self, r):
            if self._op_running:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(CommitAllDialog(
                parent, [r],
                cfg.get("commit_ram_mb", 2048),
                cfg.get("commit_max_workers", 0),
                cfg.get("commit_timeout", 3600),
                cfg.get("commit_budget_usd", 10.0),
                provider_selection(cfg), "push",
                verb="Push", verb_ing="Pushing", verb_past="Pushed",
                row_suffix=lambda rr: f"+{rr.get('unpushed', 0)}"))

        def _on_push_claude_all(self):
            if self._op_running:
                return
            repos = [r for r in self.repos
                     if r["has_remote"] and r["unpushed"] > 0]
            if not repos:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(CommitAllDialog(
                parent, repos,
                cfg.get("commit_ram_mb", 2048),
                cfg.get("commit_max_workers", 0),
                cfg.get("commit_timeout", 3600),
                cfg.get("commit_budget_usd", 10.0),
                provider_selection(cfg), "push",
                verb="Push", verb_ing="Pushing", verb_past="Pushed",
                row_suffix=lambda rr: f"+{rr.get('unpushed', 0)}"))

        def _on_commit_and_push_repo(self, r):
            if self._op_running:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(CommitAllDialog(
                parent, [r],
                cfg.get("commit_ram_mb", 2048),
                cfg.get("commit_max_workers", 0),
                cfg.get("commit_timeout", 3600),
                cfg.get("commit_budget_usd", 10.0),
                provider_selection(cfg), "commit_and_push",
                verb="Commit & Push", verb_ing="Committing & pushing",
                verb_past="Committed & pushed",
                row_suffix=lambda rr: f"{rr.get('count', '')}"))

        def _on_explain_repo(self, r):
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            dlg = ExplainDialog(parent, r,
                                cfg.get("commit_budget_usd", 10.0),
                                provider_selection(cfg))
            response = dlg.run()
            dlg.destroy()
            if response == ExplainDialog.RESPONSE_COMMIT:
                self._on_commit_repo(r)
            elif response == ExplainDialog.RESPONSE_PUSH:
                self._on_push_claude_repo(r)
            elif response == ExplainDialog.RESPONSE_COMMIT_PUSH:
                self._on_commit_and_push_repo(r)

        def _on_wt_push_claude(self, wt, repo):
            if self._op_running:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            r = {
                "path": wt["path"],
                "name": wt.get("branch", os.path.basename(wt["path"])),
                "branch": wt.get("branch", ""),
                "unpushed": 0,
            }
            self._run_op_dialog(CommitAllDialog(
                parent, [r],
                cfg.get("commit_ram_mb", 2048),
                cfg.get("commit_max_workers", 0),
                cfg.get("commit_timeout", 3600),
                cfg.get("commit_budget_usd", 10.0),
                provider_selection(cfg), "push",
                verb="Push", verb_ing="Pushing", verb_past="Pushed",
                row_suffix=lambda rr: rr.get("branch", "")))

        def _on_help(self):
            parent = self.window if (self.window and self.window.get_visible()) else None
            try:
                dlg = HelpDialog(parent)
                dlg.run()
                dlg.destroy()
            except Exception:
                import traceback
                traceback.print_exc()
                self._show_error(
                    "Help could not be opened",
                    "A bug in the help dialog construction prevented it "
                    "from being shown. The error has been printed to the "
                    "terminal.")

        def _on_about(self):
            parent = self.window if (self.window and self.window.get_visible()) else None
            try:
                dlg = Gtk.AboutDialog(transient_for=parent, modal=True)
                dlg.set_program_name("repodash")
                dlg.set_version(VERSION)
                dlg.set_comments(
                    "A tray companion for your git repositories.\n"
                    "Monitors dirty repos, unpushed commits, and stale\n"
                    "worktrees — and launches AI CLI actions (Claude Code,\n"
                    "OpenCode, Codex) from the menu."
                )
                dlg.set_copyright("© 2026 repodash contributors")
                dlg.set_license_type(Gtk.License.GPL_3_0)
                dlg.set_authors(["repodash contributors"])
                dlg.set_website("https://github.com/sicambria/repodash")
                dlg.set_website_label("github.com/sicambria/repodash")
                if os.path.isfile(ICON_SVG):
                    try:
                        from gi.repository import GdkPixbuf
                        pb = GdkPixbuf.Pixbuf.new_from_file_at_size(ICON_SVG, 64, 64)
                        dlg.set_logo(pb)
                    except Exception:
                        pass
                dlg.run()
                dlg.destroy()
            except Exception:
                import traceback
                traceback.print_exc()
                self._show_error(
                    "About could not be opened",
                    "A bug in the about dialog construction prevented it "
                    "from being shown. The error has been printed to the "
                    "terminal.")

        def _ai_label(self):
            pid = self.config.get("ai_primary_provider", "claude")
            return PROVIDERS.get(pid, PROVIDERS["claude"]).label

        def _repo_item(self, r, unpushed=False):
            if unpushed:
                # In the unpushed section, lead with the unpushed-commit count
                # (a clean-but-unpushed repo has no dirty files to show).
                label = f"{r['name']}  ({r['branch']}, +{r['unpushed']})"
            else:
                track = ""
                if r["ahead"] or r["behind"]:
                    track = f" ▲{r['ahead']}▼{r['behind']}"
                label = f"{r['name']}  ({r['branch']}{track}, {r['count']})"
            item = Gtk.MenuItem(label=label)
            sub = Gtk.Menu()
            path = r["path"]
            ai_label = self._ai_label()
            pid = self.config.get("ai_primary_provider", "claude")
            self._action(sub, "Open terminal",
                         lambda *_: notify(self.window, *open_terminal(path)))
            self._action(sub, f"Open {ai_label}",
                         lambda *_: notify(self.window, *open_provider_terminal(path, pid)))
            if explain_actions(r):
                self._action(sub, "Explain changes…",
                             lambda *_, r=r: self._on_explain_repo(r))
            commit_label = "git commit" + (f" ({r['count']})" if r["count"] else "")
            self._action(sub, commit_label,
                         lambda *_: notify(self.window, *open_commit(path)))
            if r["count"]:
                self._action(sub, f"Commit via {ai_label}…",
                             lambda *_, r=r: self._on_commit_repo(r))
            push_label = "git push" + (f" (+{r['ahead']})" if r["ahead"] else "")
            self._action(sub, push_label,
                         lambda *_: notify(self.window, *open_push(path)))
            if r.get("has_remote") and r.get("unpushed", 0) > 0:
                self._action(sub, f"Push via {ai_label}…",
                             lambda *_, r=r: self._on_push_claude_repo(r))
            if r.get("github"):
                self._action(sub, "Open GitHub",
                             lambda *_: notify(self.window, *open_github(path)))
            self._action(sub, "Open folder",
                         lambda *_: notify(self.window, *open_folder(path)))
            self._action(sub, "Copy path", lambda *_: self._copy(path))
            item.set_submenu(sub)
            return item

        def _worktree_item(self, wt, severity, r):
            age_str = _format_age(wt["last_commit_age_hours"])
            if severity == "stuck":
                label = f"  {wt['branch']}  {age_str} ago (dirty)"
            elif severity == "merged":
                label = f"  {wt['branch']}  {age_str} ago (absorbed in main)"
            else:
                behind_s = f"  ▼{wt['behind']}" if wt["behind"] else ""
                label = f"  {wt['branch']}  {age_str} ago{behind_s}"
            item = Gtk.MenuItem(label=label)
            sub = Gtk.Menu()
            path = wt["path"]
            repo_path = r["path"]
            ai_label = self._ai_label()
            pid = self.config.get("ai_primary_provider", "claude")

            self._action(sub, "Open terminal",
                         lambda *_, p=path: notify(self.window, *open_terminal(p)))
            self._action(sub, f"Open {ai_label}",
                         lambda *_, p=path: notify(self.window, *open_provider_terminal(p, pid)))

            if severity == "stuck":
                count = len([ln for ln in
                             _git(path, "status", "--porcelain").splitlines()
                             if ln.strip()])
                commit_label = f"git commit ({count})" if count else "git commit"
                self._action(sub, commit_label,
                             lambda *_, p=path: notify(self.window, *open_commit(p)))
                ahead = wt.get("ahead", 0) or (
                    int(_git(path, "rev-list", "--count",
                             "HEAD", "--not", "--remotes").strip() or "0"))
                if ahead:
                    self._action(sub, f"git push (+{ahead})",
                                 lambda *_, p=path: notify(self.window, *open_push(p)))
                    self._action(sub, f"Push via {ai_label}…",
                                 lambda *_, w=wt, rr=r: self._on_wt_push_claude(w, rr))
                sub.append(Gtk.SeparatorMenuItem())
                self._action(sub, f"Finish & merge via {ai_label}…",
                             lambda *_, w=wt, rr=r: self._on_wt_finish(w, rr))
            elif severity == "merged":
                sub.append(Gtk.SeparatorMenuItem())
                self._action(sub, "Clean up (remove worktree + delete branch)",
                             lambda *_, w=wt, rp=repo_path: self._on_wt_cleanup(w, rp))
                self._action(sub, "Remove worktree only",
                             lambda *_, w=wt, rp=repo_path: self._on_wt_remove(w, rp))
            else:
                sub.append(Gtk.SeparatorMenuItem())
                self._action(sub, f"Close via {ai_label}…",
                             lambda *_, w=wt, rr=r: self._on_wt_close(w, rr))
                self._action(sub, "Remove worktree",
                             lambda *_, w=wt, rp=repo_path: self._on_wt_remove(w, rp))

            if r.get("github"):
                sub.append(Gtk.SeparatorMenuItem())
                self._action(sub, "Open GitHub",
                             lambda *_: notify(self.window, *open_github(repo_path)))
            sub.append(Gtk.SeparatorMenuItem())
            self._action(sub, "Open folder",
                         lambda *_, p=path: notify(self.window, *open_folder(p)))
            self._action(sub, "Copy path", lambda *_, p=path: self._copy(p))
            item.set_submenu(sub)
            return item

        def _on_wt_close(self, wt, r):
            cfg = self.config
            tmpl = cfg.get("worktree_idle_close_prompt") or IDLE_CLOSE_PROMPT
            prompt = tmpl.format(path=wt["path"], branch=wt["branch"],
                                 repo_path=r["path"])
            notify(self.window, *open_wt_provider(
                wt["path"], prompt, cfg.get("ai_primary_provider", "claude")))

        def _on_wt_finish(self, wt, r):
            cfg = self.config
            tmpl = cfg.get("worktree_stuck_finish_prompt") or STUCK_FINISH_PROMPT
            prompt = tmpl.format(path=wt["path"], branch=wt["branch"],
                                 repo_path=r["path"])
            notify(self.window, *open_wt_provider(
                wt["path"], prompt, cfg.get("ai_primary_provider", "claude")))

        def _on_wt_remove(self, wt, repo_path):
            ok, msg = remove_worktree(repo_path, wt["path"])
            notify(self.window, ok, msg or f"Removed {wt['branch']}")
            if ok:
                self.refresh_menu()

        def _on_wt_cleanup(self, wt, repo_path):
            ok, msg = remove_worktree(repo_path, wt["path"], branch=wt["branch"])
            notify(self.window, ok, msg or f"Cleaned up {wt['branch']}")
            if ok:
                self.refresh_menu()

        def _stale_repo_item(self, r, severity):
            wt_list = r["stale_worktrees"][severity]
            oldest = max(wt_list, key=lambda w: w["last_commit_age_hours"])
            n = len(wt_list)
            age_str = _format_age(oldest["last_commit_age_hours"])
            if severity == "stuck":
                label = f"⚠ {r['name']}  ({n} stuck, oldest {age_str})"
            elif severity == "merged":
                label = f"✓ {r['name']}  ({n} merged, oldest {age_str})"
            else:
                behind_s = f", {oldest['behind']} behind" if oldest["behind"] else ""
                label = f"⏸ {r['name']}  ({n} idle, oldest {age_str}{behind_s})"
            item = Gtk.MenuItem(label=label)
            sub = Gtk.Menu()
            for wt in wt_list:
                sub.append(self._worktree_item(wt, severity, r))
            item.set_submenu(sub)
            return item

        @staticmethod
        def _action(menu, label, handler):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", handler)
            menu.append(item)
            return item

        def _copy(self, text):
            notify(self.window, *copy_to_clipboard(text))

        # -- dashboard tier --
        def show_dashboard(self):
            try:
                if self.window is None:
                    self.window = DashboardWindow(self, self.config)
                self.window.show_all()
                self.window.present()
                self.window.reload()
            except Exception:
                import traceback
                traceback.print_exc()
                self._show_error(
                    "Dashboard could not be opened",
                    "A bug in the dashboard window construction prevented it "
                    "from being shown. The error has been printed to the "
                    "terminal.")

    class DashboardWindow(Gtk.Window):
        def __init__(self, app, config):
            super().__init__(title="repodash")
            self.app = app
            self.config = config
            self._loading = False  # guards against overlapping reloads
            self.set_default_size(720, 560)
            self.set_icon_name("utilities-terminal")
            self.connect("delete-event", self._on_close)

            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            outer.set_border_width(8)
            self.add(outer)

            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self.search = Gtk.SearchEntry()
            self.search.set_placeholder_text("Filter by name or path…")
            self.search.connect("search-changed", lambda *_: self._refilter())
            bar.pack_start(self.search, True, True, 0)

            self.dirty_only = Gtk.CheckButton(label="Dirty only")
            self.dirty_only.connect("toggled", lambda *_: self._refilter())
            bar.pack_start(self.dirty_only, False, False, 0)
            self.has_todos = Gtk.CheckButton(label="Has TODOs")
            self.has_todos.connect("toggled", lambda *_: self._refilter())
            bar.pack_start(self.has_todos, False, False, 0)

            self.refresh_btn = Gtk.Button(label="Refresh")
            self.refresh_btn.connect("clicked", lambda *_: self.reload())
            bar.pack_start(self.refresh_btn, False, False, 0)

            settings_btn = Gtk.Button(label="Settings…")
            settings_btn.connect("clicked", lambda *_: self.app._on_settings())
            bar.pack_start(settings_btn, False, False, 0)

            outer.pack_start(bar, False, False, 0)

            self.status = Gtk.Label(label="", xalign=0)
            outer.pack_start(self.status, False, False, 0)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.listbox = Gtk.ListBox()
            self.listbox.set_filter_func(self._filter_row)
            scroller.add(self.listbox)
            outer.pack_start(scroller, True, True, 0)

        def _on_close(self, *_):
            self.hide()
            return True  # keep the app alive; just hide

        def set_config(self, config):
            self.config = config

        def reload(self):
            # Ignore re-entry: a running scan holds a repodash.py --json
            # subprocess, and overlapping reloads would race to repopulate
            # the listbox. The button is also disabled for the duration.
            if self._loading:
                return
            self._loading = True
            self.refresh_btn.set_sensitive(False)
            self.refresh_btn.set_label("Refresh (…)")
            self.status.set_text("Scanning…")
            cfg = self.config

            def work():
                model = fetch_model()
                excluded = set(cfg.get("excluded_repos", []))
                model["repos"] = [
                    r for r in model.get("repos", [])
                    if r.get("path") not in excluded
                ]
                # Resolve GitHub URLs here (off the GTK thread) and stash them
                # on each repo so row-building never blocks on git.
                for repo in model["repos"]:
                    repo["github"] = github_url(repo.get("path", ""))
                GLib.idle_add(self._populate, model)
            threading.Thread(target=work, daemon=True).start()

        def _populate(self, model):
            for child in self.listbox.get_children():
                self.listbox.remove(child)
            self._loading = False
            self.refresh_btn.set_sensitive(True)
            if model.get("error"):
                self.refresh_btn.set_label("Refresh")
                self.status.set_text("Error: " + model["error"])
                return False
            repos = model.get("repos", [])
            for repo in repos:
                self.listbox.add(self._row(repo))
            self.listbox.show_all()
            # Show the result count in brackets on the button as a completion cue.
            self.refresh_btn.set_label(f"Refresh ({len(repos)})")
            self.status.set_text(f"{len(repos)} repos")
            self._refilter()
            return False

        def _row(self, repo):
            row = Gtk.ListBoxRow()
            row._repo = repo  # stash for the filter
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_border_width(6)

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            git = repo.get("git", {})
            todos = repo.get("todos", {})
            track = ""
            if git.get("ahead") or git.get("behind"):
                track = f"  ▲{git.get('ahead', 0)}▼{git.get('behind', 0)}"
            dirty_n = len(git.get("dirty_files", []))
            title = Gtk.Label(xalign=0)
            mark = "●" if git.get("dirty") else "○"
            title.set_markup(
                f"<b>{GLib.markup_escape_text(repo.get('name', '?'))}</b>  "
                f"<small>{mark} {GLib.markup_escape_text(git.get('branch', ''))}"
                f"{track}</small>")
            info.pack_start(title, False, False, 0)

            bits = []
            if dirty_n:
                bits.append(f"{dirty_n} changed")
            if todos.get("total"):
                bits.append(f"{todos['total']} TODO")
            audit = repo.get("audit", {})
            if audit.get("files") or audit.get("archive"):
                bits.append("audit")
            if any(f.get("items") for f in repo.get("roadmap", {}).get("files", [])):
                bits.append("roadmap")
            sonar = repo.get("sonar", {})
            if sonar.get("configured"):
                bits.append("sonar" if sonar.get("ok") else "sonar!")
            sub = Gtk.Label(xalign=0)
            sub.set_markup(f"<small>{GLib.markup_escape_text(' · '.join(bits) or 'clean')}</small>")
            info.pack_start(sub, False, False, 0)
            box.pack_start(info, True, True, 0)

            path = repo.get("path", "")
            box.pack_start(self._btn("utilities-terminal", "Terminal",
                                     lambda *_: notify(self, *open_terminal(path))),
                           False, False, 0)
            pid = self.config.get("ai_primary_provider", "claude")
            ai_label = PROVIDERS.get(pid, PROVIDERS["claude"]).label
            box.pack_start(self._btn("system-run", ai_label,
                                     lambda *_: notify(self, *open_provider_terminal(path, pid))),
                           False, False, 0)
            push_tip = "git push" + (f" (▲{git.get('ahead')})" if git.get("ahead") else "")
            box.pack_start(self._btn("go-up", push_tip,
                                     lambda *_: notify(self, *open_push(path))),
                           False, False, 0)
            if repo.get("github"):
                box.pack_start(self._btn("web-browser", "GitHub",
                                         lambda *_: notify(self, *open_github(path))),
                               False, False, 0)
            box.pack_start(self._btn("folder", "Open folder",
                                     lambda *_: notify(self, *open_folder(path))),
                           False, False, 0)

            row.add(box)
            return row

        @staticmethod
        def _btn(icon_name, tooltip, handler):
            btn = Gtk.Button()
            btn.set_image(Gtk.Image.new_from_icon_name(
                icon_name, Gtk.IconSize.BUTTON))
            btn.set_tooltip_text(tooltip)
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.connect("clicked", handler)
            return btn

        def _refilter(self):
            self.listbox.invalidate_filter()

        def _filter_row(self, row):
            repo = getattr(row, "_repo", None)
            if repo is None:
                return True
            text = self.search.get_text().strip().lower()
            if text and text not in repo.get("name", "").lower() \
                    and text not in repo.get("path", "").lower():
                return False
            if self.dirty_only.get_active() and not repo.get("git", {}).get("dirty"):
                return False
            if self.has_todos.get_active() and not repo.get("todos", {}).get("total"):
                return False
            return True

    class PushAllDialog(Gtk.Dialog):
        """Modal progress window for pushing every unpushed repo in sequence.

        Streaming git output appears live in the Details expander. A Stop button
        lets the user abort mid-run (after a confirmation prompt); Close stays
        disabled until every push has finished or been cancelled.
        """

        PENDING, RUNNING, OK, FAIL, STOPPED = "·", "↻", "✓", "✗", "⊘"

        def __init__(self, parent, repos):
            super().__init__(title="Push all", transient_for=parent, modal=True)
            self._repos = repos
            self._marks = {}  # path -> status Gtk.Label
            self._done = False
            self._cancel = threading.Event()
            self._proc = None
            self._proc_lock = threading.Lock()
            self.add_button("Close", Gtk.ResponseType.CLOSE)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, False)
            self.connect("delete-event", lambda *_: not self._done)
            self.set_default_size(*_screen_fraction_size(parent))

            area = self.get_content_area()
            area.set_border_width(10)
            area.set_spacing(8)

            self._summary = Gtk.Label(
                label=f"Pushing {len(repos)} repo(s)…", xalign=0)
            area.pack_start(self._summary, False, False, 0)

            self._bar = Gtk.ProgressBar()
            self._bar.set_show_text(True)
            self._bar.set_text(f"0 / {len(repos)}")
            area.pack_start(self._bar, False, False, 0)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            listbox = Gtk.ListBox()
            listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            for r in repos:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                box.set_border_width(4)
                mark = Gtk.Label(label=self.PENDING)
                name = Gtk.Label(
                    label=f"{r['name']}  ({r['branch']}, +{r['unpushed']})",
                    xalign=0)
                box.pack_start(mark, False, False, 0)
                box.pack_start(name, True, True, 0)
                row.add(box)
                listbox.add(row)
                self._marks[r["path"]] = mark
            scroller.add(listbox)
            area.pack_start(scroller, True, True, 0)

            self._log = Gtk.TextView()
            self._log.set_editable(False)
            self._log.set_cursor_visible(False)
            self._log.set_monospace(True)
            self._log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            log_scroll = Gtk.ScrolledWindow()
            log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                                  Gtk.PolicyType.AUTOMATIC)
            log_scroll.set_min_content_height(150)
            log_scroll.add(self._log)
            expander = Gtk.Expander(label="Details")
            expander.add(log_scroll)
            expander.set_expanded(True)
            area.pack_start(expander, False, False, 0)

            # Stop button added to the action area directly so it does NOT
            # emit a dialog response (which would cause run() to return).
            action_area = self.get_action_area()
            self._stop_btn = Gtk.Button.new_with_label("Stop")
            self._stop_btn.connect("clicked", self._on_stop)
            action_area.pack_start(self._stop_btn, False, False, 0)
            action_area.reorder_child(self._stop_btn, 0)

            self.show_all()
            self._start()

        def _on_stop(self, *_):
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Stop pushing?")
            dlg.format_secondary_text(
                "The current push will be killed. Any partially-pushed refs "
                "may be in an inconsistent state.")
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.YES:
                return
            self._cancel.set()
            with self._proc_lock:
                if self._proc is not None:
                    self._killpg(self._proc)
            self._stop_btn.set_sensitive(False)

        @staticmethod
        def _killpg(proc):
            import signal as _sig
            try:
                os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

        def _start(self):
            env = {**os.environ, **_NONINTERACTIVE_GIT_ENV}

            def work():
                ok = 0
                total = len(self._repos)
                for i, r in enumerate(self._repos, 1):
                    if self._cancel.is_set():
                        GLib.idle_add(self._step, i, total, r, False, True)
                        continue

                    GLib.idle_add(self._mark, r["path"], self.RUNNING)
                    GLib.idle_add(self._append_log, f"=== {r['name']} ===\n")

                    if _current_upstream(r["path"], env):
                        argv = ["git", "-C", r["path"], "push", "--progress"]
                    else:
                        remote = next(iter(_git(r["path"], "remote").split()), "")
                        if not remote:
                            GLib.idle_add(self._append_log, "no remote configured\n")
                            GLib.idle_add(self._step, i, total, r, False, False)
                            continue
                        argv = ["git", "-C", r["path"], "push", "--progress",
                                "-u", remote, "HEAD"]

                    try:
                        proc = subprocess.Popen(
                            argv, env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
                    except (OSError, subprocess.SubprocessError) as e:
                        GLib.idle_add(self._append_log, f"Error: {e}\n")
                        GLib.idle_add(self._step, i, total, r, False, False)
                        continue

                    with self._proc_lock:
                        self._proc = proc

                    try:
                        for raw in proc.stdout:
                            line = raw.replace("\r", "\n")
                            GLib.idle_add(self._append_log, line)
                            if self._cancel.is_set():
                                self._killpg(proc)
                                GLib.idle_add(self._append_log, "[Stopped]\n")
                                break
                    except Exception:
                        pass
                    finally:
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            self._killpg(proc)
                            proc.wait()
                        with self._proc_lock:
                            self._proc = None

                    cancelled = self._cancel.is_set()
                    success = proc.returncode == 0 and not cancelled
                    ok += 1 if success else 0
                    GLib.idle_add(self._step, i, total, r, success, cancelled)

                GLib.idle_add(self._finish, ok, total)

            threading.Thread(target=work, daemon=True).start()

        def _mark(self, path, glyph):
            self._marks[path].set_text(glyph)
            return False

        def _append_log(self, text):
            buf = self._log.get_buffer()
            buf.insert(buf.get_end_iter(), text)
            end = buf.get_end_iter()
            self._log.scroll_to_iter(end, 0.0, False, 0.0, 1.0)
            return False

        def _step(self, i, total, r, success, cancelled):
            if cancelled:
                self._marks[r["path"]].set_text(self.STOPPED)
            else:
                self._marks[r["path"]].set_text(self.OK if success else self.FAIL)
            self._bar.set_fraction(i / total)
            self._bar.set_text(f"{i} / {total}")
            return False

        def _finish(self, ok, total):
            failed = total - ok
            if self._cancel.is_set():
                msg = f"Pushed {ok}/{total} · stopped"
            elif failed:
                msg = f"Pushed {ok}/{total} · {failed} failed (see Details)"
            else:
                msg = f"Pushed {ok}/{total}"
            self._summary.set_text(msg)
            self._bar.set_fraction(1.0)
            self._done = True
            self._stop_btn.set_sensitive(False)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, True)
            return False

    class CommitAllDialog(Gtk.Dialog):
        """Modal progress window for committing/pushing repos via an AI CLI.

        Bounded-parallel (ThreadPoolExecutor, RAM-derived worker count). Each
        repo's row goes ·→↻→✓/✗/⊘. The provider's output streams live into the
        Details expander. A Stop button lets the user abort mid-run (with a
        confirmation prompt) using process-group kill so no orphaned children
        are left behind.

        ``provider_sel`` (see ``provider_selection()``) selects the primary AI
        provider and an optional secondary tried once, per repo, if the
        primary is missing or its run fails/times out — gated by
        ``_repo_op_gate`` so a fallback never double-commits/double-pushes or
        hands an interrupted git operation to a second agent. ``task`` picks
        the prompt/gate semantics: "commit" | "push" | "commit_and_push".
        """

        PENDING, RUNNING, OK, FAIL, STOPPED = "·", "↻", "✓", "✗", "⊘"

        def __init__(self, parent, repos, ram_mb, cap, timeout, budget_usd,
                     provider_sel=None, task="commit",
                     verb="Commit", verb_ing="Committing", verb_past="Committed",
                     worker=None, row_suffix=None):
            title = f"{verb} {repos[0]['name']}" if len(repos) == 1 else f"{verb} all"
            super().__init__(title=title, transient_for=parent, modal=True)
            self._repos = repos
            self._timeout = timeout
            self._budget = budget_usd
            self._provider_sel = provider_sel or provider_selection(CONFIG_DEFAULTS)
            self._task = task
            self._verb_past = verb_past
            self._row_suffix = row_suffix
            self._workers = commit_workers(ram_mb, cap)
            self._marks = {}  # path → status Gtk.Label
            self._done = False
            self._cancel = threading.Event()
            self._procs = {}   # path → Popen
            self._procs_lock = threading.Lock()
            self.add_button("Close", Gtk.ResponseType.CLOSE)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, False)
            self.connect("delete-event", lambda *_: not self._done)
            self.set_default_size(*_screen_fraction_size(parent))

            area = self.get_content_area()
            area.set_border_width(10)
            area.set_spacing(8)

            summary = (f"{verb_ing} {repos[0]['name']}…" if len(repos) == 1
                       else f"{verb_ing} {len(repos)} repo(s), "
                            f"{self._workers} at a time…")
            self._summary = Gtk.Label(label=summary, xalign=0)
            area.pack_start(self._summary, False, False, 0)

            self._bar = Gtk.ProgressBar()
            self._bar.set_show_text(True)
            self._bar.set_text(f"0 / {len(repos)}")
            area.pack_start(self._bar, False, False, 0)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            listbox = Gtk.ListBox()
            listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            for r in repos:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                box.set_border_width(4)
                mark = Gtk.Label(label=self.PENDING)
                if self._row_suffix is not None:
                    row_label = f"{r['name']}  ({r['branch']}, {self._row_suffix(r)})"
                else:
                    track = ""
                    if r.get("ahead") or r.get("behind"):
                        track = f" ▲{r['ahead']}▼{r['behind']}"
                    row_label = f"{r['name']}  ({r['branch']}{track}, {r.get('count', '')})"
                name = Gtk.Label(label=row_label, xalign=0)
                box.pack_start(mark, False, False, 0)
                box.pack_start(name, True, True, 0)
                row.add(box)
                listbox.add(row)
                self._marks[r["path"]] = mark
            scroller.add(listbox)
            area.pack_start(scroller, True, True, 0)

            self._log = Gtk.TextView()
            self._log.set_editable(False)
            self._log.set_cursor_visible(False)
            self._log.set_monospace(True)
            self._log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            log_scroll = Gtk.ScrolledWindow()
            log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                                  Gtk.PolicyType.AUTOMATIC)
            log_scroll.set_min_content_height(150)
            log_scroll.add(self._log)
            expander = Gtk.Expander(label="Details")
            expander.add(log_scroll)
            expander.set_expanded(True)
            area.pack_start(expander, False, False, 0)

            action_area = self.get_action_area()
            self._stop_btn = Gtk.Button.new_with_label("Stop")
            self._stop_btn.connect("clicked", self._on_stop)
            action_area.pack_start(self._stop_btn, False, False, 0)
            action_area.reorder_child(self._stop_btn, 0)

            self.show_all()
            self._start()

        def _on_stop(self, *_):
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Stop operation?")
            dlg.format_secondary_text(
                "Running repos will be killed. Any partial commits already "
                "landed will remain. This cannot be undone.")
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.YES:
                return
            self._cancel.set()
            with self._procs_lock:
                for proc in list(self._procs.values()):
                    self._killpg(proc)
            self._stop_btn.set_sensitive(False)

        @staticmethod
        def _killpg(proc):
            import signal as _sig
            try:
                os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

        def _run_provider(self, r, provider, bin_path, multi):
            """Run one provider attempt for repo *r*. Returns True on success.

            Only called after the previous attempt (if any) for this repo has
            fully exited — attempts are sequential, never concurrent, so a
            fallback can never race the primary attempt it's replacing.
            """
            import time as _time

            model = self._provider_sel["models"].get(provider.id, "")
            effort = self._provider_sel["efforts"].get(provider.id, "")
            argv = provider.build_argv(bin_path, self._task, "stream-json",
                                       self._budget, model, effort)
            GLib.idle_add(self._append_log, f"=== {r['name']} ({provider.label}) ===\n")

            try:
                proc = subprocess.Popen(
                    argv, cwd=r["path"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True)
            except (OSError, subprocess.SubprocessError) as e:
                GLib.idle_add(self._append_log, f"[{r['name']}] Error: {e}\n")
                return False

            with self._procs_lock:
                self._procs[r["path"]] = proc

            # Kill after timeout regardless of whether we're reading.
            timeout = self._timeout

            def _kill_on_timeout():
                _time.sleep(timeout)
                if proc.poll() is None:
                    self._killpg(proc)
                    GLib.idle_add(
                        self._append_log,
                        f"[{r['name']}] timed out after {timeout}s\n")

            threading.Thread(target=_kill_on_timeout, daemon=True).start()

            try:
                for raw in proc.stdout:
                    if self._cancel.is_set():
                        self._killpg(proc)
                        GLib.idle_add(self._append_log,
                                      f"[{r['name']}] Stopped\n")
                        break
                    text = provider.parse_event(raw)
                    if text:
                        prefix = f"[{r['name']}] " if multi else ""
                        GLib.idle_add(self._append_log, prefix + text)
            except Exception:
                pass
            finally:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._killpg(proc)
                    proc.wait()
                with self._procs_lock:
                    self._procs.pop(r["path"], None)

            return proc.returncode == 0 and not self._cancel.is_set()

        def _start(self):
            from concurrent.futures import ThreadPoolExecutor, as_completed

            total = len(self._repos)
            multi = total > 1  # prefix log lines with repo name when parallel

            def one(r):
                if self._cancel.is_set():
                    return r, False
                GLib.idle_add(self._mark, r["path"], self.RUNNING)

                sel = self._provider_sel
                order = [sel["primary"]]
                if sel["fallback_enabled"] and sel["secondary"]:
                    order.append(sel["secondary"])

                for i, pid in enumerate(order):
                    provider = PROVIDERS.get(pid)
                    if provider is None or not provider.headless:
                        GLib.idle_add(
                            self._append_log,
                            f"[{r['name']}] {pid} is not available for headless runs\n")
                        continue
                    bin_path = resolve_tool_bin(provider.bin_name)
                    if not bin_path:
                        GLib.idle_add(
                            self._append_log,
                            f"[{r['name']}] {provider.bin_name} not found on PATH\n")
                        continue

                    if self._run_provider(r, provider, bin_path, multi):
                        return r, True

                    if self._cancel.is_set() or i == len(order) - 1:
                        return r, False

                    gate = _repo_op_gate(r["path"], self._task)
                    if gate == "ok_in_effect":
                        GLib.idle_add(
                            self._append_log,
                            f"[{r['name']}] {provider.label} exited non-zero but the "
                            "repo already reflects the finished work\n")
                        return r, True
                    if gate == "needs_attention":
                        GLib.idle_add(
                            self._append_log,
                            f"[{r['name']}] interrupted git operation detected after "
                            f"{provider.label} failed — needs manual review\n")
                        return r, False
                    next_provider = PROVIDERS.get(order[i + 1])
                    next_label = next_provider.label if next_provider else order[i + 1]
                    GLib.idle_add(
                        self._append_log,
                        f"[{r['name']}] {provider.label} failed — retrying with "
                        f"{next_label}\n")

                return r, False

            def work():
                ok_count = 0
                done = 0
                try:
                    with ThreadPoolExecutor(max_workers=self._workers) as ex:
                        futures = [ex.submit(one, r) for r in self._repos]
                        for fut in as_completed(futures):
                            try:
                                r, success = fut.result()
                            except Exception:
                                continue
                            ok_count += 1 if success else 0
                            done += 1
                            GLib.idle_add(self._step, done, total, r, success)
                finally:
                    GLib.idle_add(self._finish, ok_count, total)

            threading.Thread(target=work, daemon=True).start()

        def _mark(self, path, glyph):
            self._marks[path].set_text(glyph)
            return False

        def _append_log(self, text):
            buf = self._log.get_buffer()
            buf.insert(buf.get_end_iter(), text)
            end = buf.get_end_iter()
            self._log.scroll_to_iter(end, 0.0, False, 0.0, 1.0)
            return False

        def _step(self, done, total, r, success):
            if not success and self._cancel.is_set():
                self._marks[r["path"]].set_text(self.STOPPED)
            else:
                self._marks[r["path"]].set_text(self.OK if success else self.FAIL)
            self._bar.set_fraction(done / total)
            self._bar.set_text(f"{done} / {total}")
            return False

        def _finish(self, ok, total):
            failed = total - ok
            msg = f"{self._verb_past} {ok}/{total}"
            if self._cancel.is_set():
                msg += " · stopped"
            elif failed:
                msg += f" · {failed} failed"
            msg += " · see menu for final state"
            self._summary.set_text(msg)
            self._bar.set_fraction(1.0)
            self._done = True
            self._stop_btn.set_sensitive(False)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, True)
            return False

    class ExplainDialog(Gtk.Dialog):
        """Read-only "explain this repo's changes" dialog, single repo.

        Streams a headless AI-provider run (same stream-json + Popen +
        _killpg pattern as CommitAllDialog/PushAllDialog) so it can be
        cancelled and never orphans a budget-spending process. Tool-call
        chatter goes in the collapsed Details expander; the final "result"
        event becomes the prominent explanation text. Response buttons for
        Commit/Push/Commit & Push are built from explain_actions(r) and stay
        disabled until the explain run finishes.

        Explain is read-only, so — unlike commit/push — a failed primary
        attempt falls back to the secondary provider unconditionally, with no
        ``_repo_op_gate`` check (there is nothing it could double-do).
        """

        RESPONSE_COMMIT = 100
        RESPONSE_PUSH = 101
        RESPONSE_COMMIT_PUSH = 102

        def __init__(self, parent, r, budget_usd, provider_sel=None):
            super().__init__(title=f"Explain changes — {r['name']}",
                             transient_for=parent, modal=True)
            self._repo = r
            self._budget = budget_usd
            self._provider_sel = provider_sel or provider_selection(CONFIG_DEFAULTS)
            self._done = False
            self._cancel = threading.Event()
            self._proc = None
            self._proc_lock = threading.Lock()

            self.add_button("Close", Gtk.ResponseType.CLOSE)
            self._action_codes = []
            actions = explain_actions(r)
            if "commit" in actions:
                self.add_button("Commit…", self.RESPONSE_COMMIT)
                self._action_codes.append(self.RESPONSE_COMMIT)
            if "push" in actions:
                self.add_button("Push…", self.RESPONSE_PUSH)
                self._action_codes.append(self.RESPONSE_PUSH)
            if "commit_push" in actions:
                self.add_button("Commit & Push…", self.RESPONSE_COMMIT_PUSH)
                self._action_codes.append(self.RESPONSE_COMMIT_PUSH)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, False)
            for code in self._action_codes:
                self.set_response_sensitive(code, False)
            self.connect("delete-event", lambda *_: not self._done)
            self.set_default_size(*_screen_fraction_size(parent))

            area = self.get_content_area()
            area.set_border_width(10)
            area.set_spacing(8)

            bits = []
            if r.get("count"):
                bits.append(f"{r['count']} uncommitted")
            if r.get("has_remote") and r.get("unpushed", 0) > 0:
                bits.append(f"{r['unpushed']} unpushed")
            subtitle = ", ".join(bits) or "no changes"
            header = Gtk.Label(
                label=f"{r['name']}  ({r.get('branch', '')}) — {subtitle}",
                xalign=0)
            area.pack_start(header, False, False, 0)

            self._main_view = Gtk.TextView()
            self._main_view.set_editable(False)
            self._main_view.set_cursor_visible(False)
            self._main_view.set_wrap_mode(Gtk.WrapMode.WORD)
            self._main_view.set_left_margin(4)
            self._main_view.set_right_margin(4)
            self._main_view.get_buffer().set_text(
                "Asking Claude Code to explain changes…")
            main_scroll = Gtk.ScrolledWindow()
            main_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                                   Gtk.PolicyType.AUTOMATIC)
            main_scroll.add(self._main_view)
            area.pack_start(main_scroll, True, True, 0)

            self._log = Gtk.TextView()
            self._log.set_editable(False)
            self._log.set_cursor_visible(False)
            self._log.set_monospace(True)
            self._log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            log_scroll = Gtk.ScrolledWindow()
            log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                                  Gtk.PolicyType.AUTOMATIC)
            log_scroll.set_min_content_height(150)
            log_scroll.add(self._log)
            expander = Gtk.Expander(label="Details")
            expander.add(log_scroll)
            expander.set_expanded(False)
            area.pack_start(expander, False, False, 0)

            action_area = self.get_action_area()
            self._stop_btn = Gtk.Button.new_with_label("Stop")
            self._stop_btn.connect("clicked", self._on_stop)
            action_area.pack_start(self._stop_btn, False, False, 0)
            action_area.reorder_child(self._stop_btn, 0)

            self.show_all()
            self._start()

        def _on_stop(self, *_):
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Stop explaining?")
            dlg.format_secondary_text(
                "The running claude process will be killed.")
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.YES:
                return
            self._cancel.set()
            with self._proc_lock:
                if self._proc is not None:
                    self._killpg(self._proc)
            self._stop_btn.set_sensitive(False)

        @staticmethod
        def _killpg(proc):
            import signal as _sig
            try:
                os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

        def _append_log(self, text):
            buf = self._log.get_buffer()
            buf.insert(buf.get_end_iter(), text)
            end = buf.get_end_iter()
            self._log.scroll_to_iter(end, 0.0, False, 0.0, 1.0)
            return False

        def _set_result(self, text):
            self._main_view.get_buffer().set_text(text or "(no explanation returned)")
            return False

        def _finish(self):
            self._done = True
            self._stop_btn.set_sensitive(False)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, True)
            for code in self._action_codes:
                self.set_response_sensitive(code, True)
            return False

        def _run_explain_provider(self, provider, bin_path):
            """Run one explain attempt. Returns (ok, result_text)."""
            import time as _time

            model = self._provider_sel["models"].get(provider.id, "")
            effort = self._provider_sel["efforts"].get(provider.id, "")
            argv = provider.build_argv(bin_path, "explain", "stream-json",
                                       self._budget, model, effort)
            GLib.idle_add(self._append_log, f"=== {provider.label} ===\n")
            try:
                proc = subprocess.Popen(
                    argv, cwd=self._repo["path"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True)
            except (OSError, subprocess.SubprocessError) as e:
                GLib.idle_add(self._append_log, f"Error: {e}\n")
                return False, ""

            with self._proc_lock:
                self._proc = proc

            def _kill_on_timeout():
                _time.sleep(EXPLAIN_TIMEOUT)
                if proc.poll() is None:
                    self._killpg(proc)
                    GLib.idle_add(self._append_log,
                                  f"timed out after {EXPLAIN_TIMEOUT}s\n")

            threading.Thread(target=_kill_on_timeout, daemon=True).start()

            result_text = ""
            try:
                for raw in proc.stdout:
                    if self._cancel.is_set():
                        self._killpg(proc)
                        GLib.idle_add(self._append_log, "Stopped\n")
                        break
                    res = provider.extract_result(raw)
                    if res is not None:
                        result_text = res
                    text = provider.parse_event(raw)
                    if text:
                        GLib.idle_add(self._append_log, text)
            except Exception:
                pass
            finally:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._killpg(proc)
                    proc.wait()
                with self._proc_lock:
                    self._proc = None

            return proc.returncode == 0 and not self._cancel.is_set(), result_text

        def _start(self):
            def work():
                sel = self._provider_sel
                order = [sel["primary"]]
                if sel["fallback_enabled"] and sel["secondary"]:
                    order.append(sel["secondary"])

                result_text = ""
                ok = False
                for pid in order:
                    if self._cancel.is_set():
                        break
                    provider = PROVIDERS.get(pid)
                    if provider is None or not provider.headless:
                        GLib.idle_add(self._append_log,
                                      f"{pid} is not available for headless runs\n")
                        continue
                    bin_path = resolve_tool_bin(provider.bin_name)
                    if not bin_path:
                        GLib.idle_add(self._append_log,
                                      f"{provider.bin_name} not found on PATH\n")
                        continue
                    ok, text = self._run_explain_provider(provider, bin_path)
                    if text:
                        result_text = text
                    # Read-only, so — unlike commit/push — fall back on any
                    # failure with no _repo_op_gate check: there is nothing a
                    # second attempt could double-do.
                    if ok or self._cancel.is_set():
                        break

                if self._cancel.is_set() and not result_text:
                    result_text = "Stopped before finishing."
                elif not result_text and not ok:
                    result_text = "(no explanation returned — is an AI CLI installed?)"
                GLib.idle_add(self._set_result, result_text)
                GLib.idle_add(self._finish)

            threading.Thread(target=work, daemon=True).start()

    class ConfigDialog(Gtk.Dialog):
        def __init__(self, parent, config):
            super().__init__(title="Settings", transient_for=parent, modal=True)
            self.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                             "Save", Gtk.ResponseType.OK)
            self._config = dict(config)
            self._config["excluded_repos"] = list(
                config.get("excluded_repos", []))
            # Deep-copy the per-provider sub-dicts: dict(config) above only
            # shallow-copies, so without this, editing a provider's model in
            # the dialog would mutate the caller's live config in place even
            # if the user hits Cancel.
            self._config["ai_providers"] = {
                pid: dict(vals) for pid, vals
                in config.get("ai_providers", CONFIG_DEFAULTS["ai_providers"]).items()
            }
            self._repo_checks = {}    # path -> Gtk.CheckButton
            self._provider_widgets = {}  # provider id -> {"model": combo, "effort": combo|None}

            notebook = Gtk.Notebook()
            notebook.append_page(self._build_general_tab(),
                                 Gtk.Label(label="General"))
            notebook.append_page(self._build_git_tab(),
                                 Gtk.Label(label="Git"))
            notebook.append_page(self._build_repos_tab(),
                                 Gtk.Label(label="Repositories"))
            notebook.append_page(self._build_ai_tab(),
                                 Gtk.Label(label="AI"))
            for pid in ("claude", "opencode", "codex", "gemini"):
                notebook.append_page(self._build_ai_provider_tab(pid),
                                     Gtk.Label(label=PROVIDERS[pid].label))
            self.get_content_area().pack_start(notebook, True, True, 0)
            self.set_default_size(560, 560)
            self.show_all()

        def _build_general_tab(self):
            grid = Gtk.Grid()
            grid.set_row_spacing(8)
            grid.set_column_spacing(12)
            grid.set_border_width(12)

            def row(r, label_text, widget, hint=None):
                lbl = Gtk.Label(label=label_text, xalign=1.0)
                grid.attach(lbl, 0, r, 1, 1)
                grid.attach(widget, 1, r, 1, 1)
                widget.set_hexpand(True)
                if hint:
                    sub = Gtk.Label(label=hint, xalign=0.0)
                    sub.get_style_context().add_class("dim-label")
                    grid.attach(sub, 1, r + 1, 1, 1)

            self._entry_base = Gtk.Entry()
            self._entry_base.set_placeholder_text(
                f"default: {base_dir()}")
            self._entry_base.set_text(self._config.get("base_dir", ""))
            row(0, "Base directory", self._entry_base)

            adj_depth = Gtk.Adjustment(value=self._config.get("depth", 0),
                                       lower=0, upper=10, step_increment=1)
            self._spin_depth = Gtk.SpinButton(adjustment=adj_depth, digits=0)
            row(2, "Scan depth", self._spin_depth,
                "0 = use REPODASH_DEPTH env var or default (3)")

            adj_iv = Gtk.Adjustment(
                value=self._config.get("refresh_interval", 0),
                lower=0, upper=3600, step_increment=5)
            self._spin_interval = Gtk.SpinButton(adjustment=adj_iv, digits=0)
            row(4, "Refresh interval (s)", self._spin_interval,
                "0 = use REPODASH_TRAY_INTERVAL env var or default (90)")

            self._entry_terminal = Gtk.Entry()
            self._entry_terminal.set_placeholder_text(
                "default: auto-detect (ptyxis, gnome-terminal, …)")
            self._entry_terminal.set_text(self._config.get("terminal", ""))
            row(6, "Terminal", self._entry_terminal)

            return grid

        def _build_repos_tab(self):
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            vbox.set_border_width(8)

            hint = Gtk.Label(
                label="Uncheck repos to exclude them from the dashboard and tray menu.",
                xalign=0.0, wrap=True)
            vbox.pack_start(hint, False, False, 0)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self._repo_listbox = Gtk.ListBox()
            self._repo_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            scroller.add(self._repo_listbox)
            vbox.pack_start(scroller, True, True, 0)

            rescan_btn = Gtk.Button(label="Rescan")
            rescan_btn.connect("clicked", self._on_rescan)
            vbox.pack_start(rescan_btn, False, False, 0)

            self._populate_repo_list()
            return vbox

        def _populate_repo_list(self):
            for child in self._repo_listbox.get_children():
                self._repo_listbox.remove(child)
            self._repo_checks.clear()

            base = self._entry_base.get_text().strip() or base_dir()
            depth_val = int(self._spin_depth.get_value())
            depth = depth_val if depth_val > 0 else scan_depth()
            excluded = set(self._config.get("excluded_repos", []))

            def work():
                repos = find_repos(base, depth)
                GLib.idle_add(self._apply_repo_list, repos, excluded)

            threading.Thread(target=work, daemon=True).start()

        def _apply_repo_list(self, repos, excluded):
            for child in self._repo_listbox.get_children():
                self._repo_listbox.remove(child)
            self._repo_checks.clear()
            for path in repos:
                row = Gtk.ListBoxRow()
                chk = Gtk.CheckButton(label=path)
                chk.set_active(path not in excluded)
                row.add(chk)
                self._repo_listbox.add(row)
                self._repo_checks[path] = chk
            self._repo_listbox.show_all()
            return False

        def _on_rescan(self, *_):
            self._populate_repo_list()

        def _build_git_tab(self):
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            vbox.set_border_width(12)

            def section(title):
                lbl = Gtk.Label(xalign=0.0)
                # markup_escape_text is mandatory: set_markup silently renders an
                # empty label on invalid Pango XML (e.g. a bare & in a title string).
                lbl.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
                vbox.pack_start(lbl, False, False, 0)

            def spin_row(label_text, key, default, lo, hi, step, hint=None):
                hbox = Gtk.Box(spacing=8)
                lbl = Gtk.Label(label=label_text, xalign=1.0, width_chars=22)
                hbox.pack_start(lbl, False, False, 0)
                adj = Gtk.Adjustment(value=self._config.get(key, default),
                                     lower=lo, upper=hi, step_increment=step)
                spin = Gtk.SpinButton(adjustment=adj, digits=0)
                hbox.pack_start(spin, False, False, 0)
                if hint:
                    hl = Gtk.Label(label=hint, xalign=0.0)
                    hl.get_style_context().add_class("dim-label")
                    hbox.pack_start(hl, False, False, 0)
                vbox.pack_start(hbox, False, False, 0)
                return spin

            section("Repositories")
            self._chk_remoteless = Gtk.CheckButton(
                label="Show repos without a remote")
            self._chk_remoteless.set_active(
                self._config.get("show_remoteless", True))
            vbox.pack_start(self._chk_remoteless, False, False, 0)

            section("Stale worktree detection")
            self._chk_show_stale = Gtk.CheckButton(
                label="Enable stale worktree detection")
            self._chk_show_stale.set_active(
                self._config.get("show_stale_worktrees", True))
            vbox.pack_start(self._chk_show_stale, False, False, 0)

            section("⏸  Idle worktree")
            self._spin_idle_hours = spin_row(
                "Idle threshold (h):", "stale_worktree_idle_hours", 24, 1, 8760, 1,
                "clean+no-ahead worktrees older than this are idle")

            section("⚠  Stuck worktree")
            self._spin_stuck_hours = spin_row(
                "Stuck threshold (h):", "stale_worktree_stuck_hours", 12, 1, 8760, 1,
                "dirty worktrees older than this are stuck")

            outer = Gtk.ScrolledWindow()
            outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            outer.add(vbox)
            return outer

        def _build_ai_tab(self):
            """Generic AI settings: primary/secondary provider, fallback, the
            provider-agnostic run limits, and the worktree prompts (also
            provider-agnostic — whichever provider runs them gets the same
            English instructions)."""
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            vbox.set_border_width(12)

            def section(title):
                lbl = Gtk.Label(xalign=0.0)
                lbl.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
                vbox.pack_start(lbl, False, False, 0)

            def spin_row(label_text, key, default, lo, hi, step,
                        digits=0, hint=None):
                hbox = Gtk.Box(spacing=8)
                lbl = Gtk.Label(label=label_text, xalign=1.0, width_chars=22)
                hbox.pack_start(lbl, False, False, 0)
                adj = Gtk.Adjustment(value=self._config.get(key, default),
                                     lower=lo, upper=hi, step_increment=step)
                spin = Gtk.SpinButton(adjustment=adj, digits=digits)
                hbox.pack_start(spin, False, False, 0)
                if hint:
                    hl = Gtk.Label(label=hint, xalign=0.0)
                    hl.get_style_context().add_class("dim-label")
                    hbox.pack_start(hl, False, False, 0)
                vbox.pack_start(hbox, False, False, 0)
                return spin

            def prompt_row(label_text, key, default_text):
                lbl = Gtk.Label(label=label_text, xalign=0.0)
                vbox.pack_start(lbl, False, False, 0)
                sw = Gtk.ScrolledWindow()
                sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
                sw.set_size_request(-1, 100)
                tv = Gtk.TextView()
                tv.set_wrap_mode(Gtk.WrapMode.WORD)
                buf = tv.get_buffer()
                saved = self._config.get(key, "")
                buf.set_text(saved if saved else default_text)
                sw.add(tv)
                vbox.pack_start(sw, True, True, 0)
                return buf

            # ── Provider selection ────────────────────────────────────────
            section("AI provider")

            def provider_combo_row(label_text, key, allow_none, hint=None):
                hbox = Gtk.Box(spacing=8)
                lbl = Gtk.Label(label=label_text, xalign=1.0, width_chars=22)
                hbox.pack_start(lbl, False, False, 0)
                combo = Gtk.ComboBoxText()
                if allow_none:
                    combo.append("", "(none)")
                for pid in HEADLESS_PROVIDER_IDS:
                    provider = PROVIDERS[pid]
                    status = "✓ installed" if resolve_tool_bin(provider.bin_name) \
                        else "not found"
                    combo.append(pid, f"{provider.label} ({status})")
                current = self._config.get(key, "")
                if combo.set_active_id(current) is False:
                    combo.set_active_id("" if allow_none else "claude")
                hbox.pack_start(combo, False, False, 0)
                if hint:
                    hl = Gtk.Label(label=hint, xalign=0.0)
                    hl.get_style_context().add_class("dim-label")
                    hbox.pack_start(hl, False, False, 0)
                vbox.pack_start(hbox, False, False, 0)
                return combo

            self._combo_ai_primary = provider_combo_row(
                "Primary:", "ai_primary_provider", allow_none=False,
                hint="does the work for Commit/Push/Explain actions")
            self._combo_ai_secondary = provider_combo_row(
                "Secondary (fallback):", "ai_secondary_provider", allow_none=True,
                hint="tried once if the primary is missing or a run fails")
            self._chk_ai_fallback = Gtk.CheckButton(
                label="Fall back to the secondary provider on failure")
            self._chk_ai_fallback.set_active(
                self._config.get("ai_fallback_enabled", True))
            vbox.pack_start(self._chk_ai_fallback, False, False, 0)

            # ── Run limits (provider-agnostic) ────────────────────────────
            section("Run limits")
            self._spin_commit_ram = spin_row(
                "RAM/proc (MB):", "commit_ram_mb", 2048, 256, 65536, 256,
                hint="RAM budgeted per AI-provider process; workers = MemAvailable ÷ this")
            self._spin_commit_workers = spin_row(
                "Max workers:", "commit_max_workers", 0, 0, 64, 1,
                hint="0 = auto (RAM- and CPU-derived); >0 caps concurrency")
            self._spin_commit_timeout = spin_row(
                "Timeout (s):", "commit_timeout", 3600, 30, 7200, 30,
                hint="per-repo cap before a run is killed")
            self._spin_commit_budget = spin_row(
                "Budget ($/repo):", "commit_budget_usd", 10.0, 0, 1000, 1,
                digits=2, hint="max spend per repo — only Claude Code supports this")

            # ── Prompts ────────────────────────────────────────────────────
            section("⏸  Idle worktree — close prompt")
            hint_idle = Gtk.Label(
                label="Placeholders: {path}  {branch}  {repo_path}",
                xalign=0.0)
            hint_idle.get_style_context().add_class("dim-label")
            vbox.pack_start(hint_idle, False, False, 0)
            self._buf_idle_prompt = prompt_row(
                "", "worktree_idle_close_prompt", IDLE_CLOSE_PROMPT)

            section("⚠  Stuck worktree — finish & merge prompt")
            hint_stuck = Gtk.Label(
                label="Placeholders: {path}  {branch}  {repo_path}",
                xalign=0.0)
            hint_stuck.get_style_context().add_class("dim-label")
            vbox.pack_start(hint_stuck, False, False, 0)
            self._buf_stuck_prompt = prompt_row(
                "", "worktree_stuck_finish_prompt", STUCK_FINISH_PROMPT)

            outer = Gtk.ScrolledWindow()
            outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            outer.add(vbox)
            return outer

        def _build_ai_provider_tab(self, pid):
            """Model (and, if supported, effort) for one provider. Model is a
            freeform-editable combo: the seed list is suggestions, not a
            closed set (e.g. Claude Code can point at a DeepSeek/GLM endpoint
            via ANTHROPIC_BASE_URL — any model string the provider accepts
            is valid here)."""
            provider = PROVIDERS[pid]
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            vbox.set_border_width(12)
            widgets = {"model": None, "effort": None}

            def combo_entry_row(label_text, key, options, hint=None):
                hbox = Gtk.Box(spacing=8)
                lbl = Gtk.Label(label=label_text, xalign=1.0, width_chars=22)
                hbox.pack_start(lbl, False, False, 0)
                combo = Gtk.ComboBoxText.new_with_entry()
                for opt_id, _opt_label in options:
                    combo.append_text(opt_id)
                combo.set_row_separator_func(
                    lambda model, it: model[it][0] == _OPENGODE_GO_HEADER)
                current = self._config["ai_providers"].get(pid, {}).get(key, "")
                combo.get_child().set_text(current)

                store = combo.get_model()
                entry = combo.get_child()
                completion_store = Gtk.ListStore(str)
                for opt_id, _opt_label in options:
                    if opt_id != _OPENGODE_GO_HEADER:
                        completion_store.append([opt_id])
                completion = Gtk.EntryCompletion()
                completion.set_model(completion_store)
                completion.set_text_column(0)
                completion.set_minimum_key_length(1)
                completion.set_match_func(
                    lambda _c, key, it: key.lower() in completion_store[it][0].lower())
                entry.set_completion(completion)

                hbox.pack_start(combo, False, False, 0)
                if hint:
                    hl = Gtk.Label(label=hint, xalign=0.0)
                    hl.get_style_context().add_class("dim-label")
                    hbox.pack_start(hl, False, False, 0)
                vbox.pack_start(hbox, False, False, 0)
                return combo

            if not provider.headless:
                note = Gtk.Label(
                    xalign=0.0, wrap=True,
                    label=f"{provider.label} isn't wired for headless "
                          "Commit/Push/Explain yet — its JSON output format "
                          "isn't confirmed stable. You can still use "
                          f"“Open {provider.label}” for an "
                          "interactive session.")
                note.get_style_context().add_class("dim-label")
                vbox.pack_start(note, False, False, 0)

            opts = list(provider.model_options)
            if pid == "opencode":
                if not _FETCHED_OPENGODE_GO_MODELS:
                    _fetch_opencode_go_models()
                if _FETCHED_OPENGODE_GO_MODELS:
                    opts.append((_OPENGODE_GO_HEADER, ""))
                    opts.extend(_FETCHED_OPENGODE_GO_MODELS)
            widgets["model"] = combo_entry_row(
                "Model:", "model", opts,
                hint="freeform — pick a suggestion or type any model name/id")
            if provider.effort_options:
                widgets["effort"] = combo_entry_row(
                    "Effort:", "effort", provider.effort_options,
                    hint="reasoning effort for headless runs")
            if not provider.supports_budget:
                hint = Gtk.Label(
                    xalign=0.0,
                    label="No cost-budget flag for this provider — only the "
                          "AI tab's timeout applies.")
                hint.get_style_context().add_class("dim-label")
                vbox.pack_start(hint, False, False, 0)

            self._provider_widgets[pid] = widgets
            outer = Gtk.ScrolledWindow()
            outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            outer.add(vbox)
            return outer

        def get_config(self) -> dict:
            cfg = dict(self._config)
            cfg["base_dir"] = self._entry_base.get_text().strip()
            cfg["depth"] = int(self._spin_depth.get_value())
            cfg["refresh_interval"] = int(self._spin_interval.get_value())
            cfg["terminal"] = self._entry_terminal.get_text().strip()
            cfg["show_remoteless"] = self._chk_remoteless.get_active()
            cfg["commit_ram_mb"] = int(self._spin_commit_ram.get_value())
            cfg["commit_max_workers"] = int(self._spin_commit_workers.get_value())
            cfg["commit_timeout"] = int(self._spin_commit_timeout.get_value())
            cfg["commit_budget_usd"] = round(
                self._spin_commit_budget.get_value(), 2)
            cfg["ai_primary_provider"] = (
                self._combo_ai_primary.get_active_id() or "claude")
            cfg["ai_secondary_provider"] = self._combo_ai_secondary.get_active_id() or ""
            cfg["ai_fallback_enabled"] = self._chk_ai_fallback.get_active()
            ai_providers = {pid: dict(vals) for pid, vals
                           in self._config.get("ai_providers", {}).items()}
            for pid, widgets in self._provider_widgets.items():
                entry = dict(ai_providers.get(pid, {}))
                if widgets.get("model") is not None:
                    entry["model"] = widgets["model"].get_child().get_text().strip()
                if widgets.get("effort") is not None:
                    entry["effort"] = widgets["effort"].get_child().get_text().strip()
                ai_providers[pid] = entry
            cfg["ai_providers"] = ai_providers
            cfg["show_stale_worktrees"] = self._chk_show_stale.get_active()
            cfg["stale_worktree_idle_hours"] = int(self._spin_idle_hours.get_value())
            cfg["stale_worktree_stuck_hours"] = int(self._spin_stuck_hours.get_value())
            start, end = self._buf_idle_prompt.get_bounds()
            idle_text = self._buf_idle_prompt.get_text(start, end, False).strip()
            cfg["worktree_idle_close_prompt"] = (
                "" if idle_text == IDLE_CLOSE_PROMPT.strip() else idle_text)
            start, end = self._buf_stuck_prompt.get_bounds()
            stuck_text = self._buf_stuck_prompt.get_text(start, end, False).strip()
            cfg["worktree_stuck_finish_prompt"] = (
                "" if stuck_text == STUCK_FINISH_PROMPT.strip() else stuck_text)
            # Repos unchecked in the current scan list.
            shown_excluded = {
                path for path, chk in self._repo_checks.items()
                if not chk.get_active()
            }
            # Preserve exclusions for repos not shown in the current scan
            # (e.g. after a base_dir change the old paths aren't visible).
            old_excluded = set(self._config.get("excluded_repos", []))
            not_shown = old_excluded - set(self._repo_checks.keys())
            cfg["excluded_repos"] = sorted(shown_excluded | not_shown)
            return cfg

    class HelpDialog(Gtk.Dialog):
        _CONTENT = [
            ("h", "repodash Workflow Guide"),
            ("p", "repodash watches your git repos and surfaces work that needs "
                  "attention. The tray icon shows a count of dirty repos; click "
                  "an entry to act on it."),
            ("h2", "Dirty repos (changed files)"),
            ("p", "Repos with uncommitted changes appear first, marked with ●."),
            ("item", "Open terminal",
             "Opens a terminal in the repo directory."),
            ("item", "Open <AI provider>",
             "Opens your configured primary AI CLI (Claude Code, OpenCode, "
             "Codex, or Gemini CLI) interactively in a terminal."),
            ("item", "git commit (N)",
             "Opens a terminal with your editor so you can write the commit "
             "message yourself. Good for quick, focused commits."),
            ("item", "Commit via <AI provider>…",
             "Your primary AI provider inspects the diff, groups changes into "
             "logical commits with appropriate messages, fixes any pre-commit "
             "hook failures, then optionally merges the branch into main. If "
             "it's missing or the run fails, a configured secondary provider "
             "is tried once — only after re-checking the repo shows work "
             "genuinely remains, so a fallback never double-commits. A "
             "progress dialog shows per-repo status."),
            ("h2", "Unpushed repos"),
            ("p", "Repos with local commits not yet on a remote appear in the "
                  "Unpushed section."),
            ("item", "git push",
             "Opens a terminal and runs git push. Use this when you need to "
             "enter a passphrase or watch the output interactively."),
            ("item", "Push via <AI provider>…",
             "Your primary AI provider runs git push, handles non-fast-forward "
             "divergence (pull --rebase + retry), and fixes pre-push hook "
             "failures — with the same safe fallback-to-secondary behavior as "
             "Commit. Use when a plain push fails and you want errors fixed "
             "automatically."),
            ("h2", "Stale worktrees"),
            ("p", "Extra git worktrees (from git worktree add) that have gone "
                  "quiet appear as ⚠ Stuck or ⏸ Idle sections."),
            ("item", "⚠ Stuck",
             "A worktree with uncommitted changes sitting idle longer than the "
             "configured threshold. Use “Finish & merge via <AI provider>” "
             "to commit, merge into main, and remove the worktree automatically."),
            ("item", "⏸ Idle",
             "A clean worktree with no ahead commits sitting idle. Use "
             "“Close via <AI provider>” to review and remove it, or "
             "“Remove worktree” for an immediate direct delete."),
            ("h2", "Dashboard"),
            ("p", "Lists every repo with full status. Open with "
                  "“Show dashboard…” or by re-launching the tray. "
                  "Each row has buttons for Terminal, your AI provider, Push, "
                  "GitHub, and Open folder."),
            ("h2", "Settings"),
            ("item", "General",
             "Scan root directory, depth, refresh interval, terminal."),
            ("item", "Git",
             "Show/hide remoteless repos; stale-worktree thresholds."),
            ("item", "Repositories",
             "Per-repo include/exclude list. Rescan after changing the root."),
            ("item", "AI",
             "Pick a primary AI CLI provider (Claude Code, OpenCode, Codex — "
             "each shown with its install status) and an optional secondary "
             "tried once as a fallback if the primary is missing or a run "
             "fails/times out. Also holds the provider-agnostic run limits "
             "(RAM/worker/timeout/budget — budget only applies to Claude "
             "Code) and the customisable worktree close/finish prompts. "
             "Placeholders {path}, {branch}, {repo_path} are substituted at "
             "runtime."),
            ("item", "Claude Code / OpenCode / Codex / Gemini",
             "Model and (where the provider supports it) reasoning-effort "
             "level for that provider's headless runs. Model is freeform — "
             "type any model name/id the provider accepts, e.g. a DeepSeek or "
             "GLM endpoint via Claude Code's ANTHROPIC_BASE_URL. Gemini CLI "
             "is detected and launchable interactively but not yet wired for "
             "headless Commit/Push/Explain."),
        ]

        def __init__(self, parent):
            super().__init__(title="repodash — Help",
                             transient_for=parent, modal=True)
            self.add_button("Close", Gtk.ResponseType.CLOSE)
            self.set_default_size(580, 500)

            area = self.get_content_area()
            area.set_border_width(0)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

            tv = Gtk.TextView()
            tv.set_editable(False)
            tv.set_cursor_visible(False)
            tv.set_wrap_mode(Gtk.WrapMode.WORD)
            tv.set_left_margin(16)
            tv.set_right_margin(16)
            tv.set_top_margin(12)
            tv.set_bottom_margin(12)

            buf = tv.get_buffer()
            t_h = buf.create_tag("h", weight=700, scale=1.2,
                                 pixels_above_lines=4, pixels_below_lines=4)
            t_h2 = buf.create_tag("h2", weight=700, pixels_above_lines=10)
            t_bold = buf.create_tag("bold", weight=700)
            t_dim = buf.create_tag("dim", foreground="#888888")

            def ins(text, *tags):
                end = buf.get_end_iter()
                active = [t for t in tags if t is not None]
                if active:
                    buf.insert_with_tags(end, text, *active)
                else:
                    buf.insert(end, text)

            for entry in self._CONTENT:
                kind = entry[0]
                if kind == "h":
                    ins(entry[1] + "\n", t_h)
                elif kind == "h2":
                    ins("\n" + entry[1] + "\n", t_h2)
                elif kind == "p":
                    ins(entry[1] + "\n")
                elif kind == "item":
                    ins("  " + entry[1], t_bold)
                    ins("\n    " + entry[2] + "\n", t_dim)

            scroller.add(tv)
            area.pack_start(scroller, True, True, 0)
            self.show_all()

    app = TrayApp()
    return app.run([sys.argv[0]])


# ── entry point ──────────────────────────────────────────────────────────────
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    if "--check" in argv:
        return run_check()
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())

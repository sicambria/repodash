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
    with quick actions (terminal, Claude Code, GitHub, folder, copy path);
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

# ── configuration ────────────────────────────────────────────────────────────
DEFAULT_DEPTH = 3
DEFAULT_INTERVAL = 90  # seconds between cheap menu refreshes
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
    "commit_ram_mb": 2048,      # RAM budget per claude process (MB)
    "commit_max_workers": 0,    # 0 = auto (RAM/CPU derived); >0 = hard cap
    "commit_timeout": 900,      # seconds per repo (agentic runs are slow)
    "commit_budget_usd": 10.0,  # max $ a single repo's claude run may spend
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
    try:
        with open(config_file(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for key in CONFIG_DEFAULTS:
                if key in saved:
                    cfg[key] = saved[key]
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
    """Scan extra worktrees of *repo_path* for stuck/idle states.

    Returns {"stuck": [...], "idle": [...]}.
    Each entry: {path, branch, last_commit_age_hours, behind, dirty}.

    Stuck: dirty == True  AND last_commit_age_hours > stuck_hours
    Idle:  dirty == False AND ahead == 0 AND last_commit_age_hours > idle_hours
    """
    import time
    result: dict = {"stuck": [], "idle": []}
    raw = _git(repo_path, "worktree", "list", "--porcelain")
    if not raw:
        return result
    worktrees = _parse_worktree_list(raw)
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
        elif not dirty and ahead == 0 and age_hours > idle_hours:
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


def remove_worktree(repo_path: str, wt_path: str):
    """Remove a git worktree directly (no Claude). Returns (ok, message)."""
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, "worktree", "remove", wt_path],
            capture_output=True, text=True, timeout=15)
        return out.returncode == 0, (out.stdout + out.stderr).strip()
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


def push_claude_argv(bin_path: str, budget_usd: float) -> list:
    """argv to run claude headlessly with PUSH_PROMPT, bounded by a $ budget."""
    argv = [bin_path, "-p", PUSH_PROMPT,
            "--dangerously-skip-permissions",
            "--output-format", "json"]
    if budget_usd and float(budget_usd) > 0:
        argv += ["--max-budget-usd", str(float(budget_usd))]
    return argv


def push_claude_repo(path: str, timeout: int = 900, budget_usd: float = 10.0):
    """Run claude in *path* to push via headless Claude. Returns (ok, output). Never raises."""
    bin_path = shutil.which(CLAUDE_BIN)
    if not bin_path:
        return False, "claude not found on PATH"
    try:
        out = subprocess.run(push_claude_argv(bin_path, budget_usd), cwd=path,
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


def commit_argv(bin_path: str, budget_usd: float) -> list:
    """argv to run claude headlessly with COMMIT_PROMPT, bounded by a $ budget."""
    argv = [bin_path, "-p", COMMIT_PROMPT,
            "--dangerously-skip-permissions",
            "--output-format", "json"]
    if budget_usd and float(budget_usd) > 0:
        argv += ["--max-budget-usd", str(float(budget_usd))]
    return argv


def commit_repo(path: str, timeout: int = 900, budget_usd: float = 10.0):
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
        out = subprocess.run(commit_argv(bin_path, budget_usd), cwd=path,
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
    claude_bin = shutil.which(CLAUDE_BIN) or "(not on PATH)"
    print(f"commit    : {ram_mb} MB/proc, {cap_desc} → {workers} workers, "
          f"{cfg.get('commit_timeout', 900)}s timeout, "
          f"${cfg.get('commit_budget_usd', 10.0)}/repo  [{avail_desc}]")
    print(f"  claude   : {claude_bin}")

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
            track = f"  ↑{r['ahead']} ↓{r['behind']}"
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
            behind_s = f"  ↓{w['behind']}" if w["behind"] else ""
            print(f"  ⏸ {r['name']}  [{w['branch']}]  "
                  f"{_format_age(w['last_commit_age_hours'])} ago{behind_s}")
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
            self.config = load_config()
            apply_config_to_env(self.config)

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
                            or sw.get("stuck") or sw.get("idle")):
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
                self._action(menu, f"Commit all ({len(dirty)})…",
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
                self._action(menu, f"Push all via Claude Code ({len(unpushed)})…",
                             lambda *_: self._on_push_claude_all())

            stuck_repos = [r for r in self.repos
                           if r.get("stale_worktrees", {}).get("stuck")]
            idle_repos = [r for r in self.repos
                          if r.get("stale_worktrees", {}).get("idle")]

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

            menu.append(Gtk.SeparatorMenuItem())
            self._action(menu, "Show dashboard…",
                         lambda *_: self.show_dashboard())
            self._action(menu, "Refresh now", lambda *_: self.refresh_menu())
            self._action(menu, "Settings…", lambda *_: self._on_settings())
            self._action(menu, "Help…", lambda *_: self._on_help())

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

        def _on_settings(self):
            parent = self.window if (self.window and self.window.get_visible()) else None
            dlg = ConfigDialog(parent, self.config)
            response = dlg.run()
            if response == Gtk.ResponseType.OK:
                self.config = dlg.get_config()
                apply_config_to_env(self.config)
                save_config(self.config)
                # Re-arm the refresh timer with the (possibly new) interval.
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

        def _on_push_all(self):
            # Recompute from the latest scan (the menu may have refreshed since
            # it was built) so we never push a stale set.
            repos = [r for r in self.repos
                     if r["has_remote"] and r["unpushed"] > 0]
            if not repos:
                return
            parent = self.window if (self.window and self.window.get_visible()) else None
            dlg = PushAllDialog(parent, repos)
            dlg.run()
            dlg.destroy()
            self.refresh_menu()  # reflect the now-pushed repos

        def _on_commit_all(self):
            # Recompute the dirty set from the latest scan so a refresh between
            # menu-build and click never commits a stale list.
            repos = [r for r in self.repos if r["dirty"]]
            if not repos:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            dlg = CommitAllDialog(parent, repos,
                                  cfg.get("commit_ram_mb", 2048),
                                  cfg.get("commit_max_workers", 0),
                                  cfg.get("commit_timeout", 900),
                                  cfg.get("commit_budget_usd", 10.0))
            dlg.run()
            dlg.destroy()
            self.refresh_menu()  # reflect now-clean / merged repos

        def _on_commit_repo(self, r):
            # Single-repo counterpart to _on_commit_all: same headless-Claude
            # flow (logical chunks, repo-conventional messages, docs, merge),
            # just scoped to one repo via the shared progress dialog.
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            dlg = CommitAllDialog(parent, [r],
                                  cfg.get("commit_ram_mb", 2048),
                                  cfg.get("commit_max_workers", 0),
                                  cfg.get("commit_timeout", 900),
                                  cfg.get("commit_budget_usd", 10.0))
            dlg.run()
            dlg.destroy()
            self.refresh_menu()  # reflect now-clean / merged repo

        def _on_push_claude_repo(self, r):
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            dlg = CommitAllDialog(
                parent, [r],
                cfg.get("commit_ram_mb", 2048),
                cfg.get("commit_max_workers", 0),
                cfg.get("commit_timeout", 900),
                cfg.get("commit_budget_usd", 10.0),
                verb="Push", verb_ing="Pushing", verb_past="Pushed",
                worker=push_claude_repo,
                row_suffix=lambda rr: f"+{rr.get('unpushed', 0)}")
            dlg.run()
            dlg.destroy()
            self.refresh_menu()

        def _on_push_claude_all(self):
            repos = [r for r in self.repos
                     if r["has_remote"] and r["unpushed"] > 0]
            if not repos:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            dlg = CommitAllDialog(
                parent, repos,
                cfg.get("commit_ram_mb", 2048),
                cfg.get("commit_max_workers", 0),
                cfg.get("commit_timeout", 900),
                cfg.get("commit_budget_usd", 10.0),
                verb="Push", verb_ing="Pushing", verb_past="Pushed",
                worker=push_claude_repo,
                row_suffix=lambda rr: f"+{rr.get('unpushed', 0)}")
            dlg.run()
            dlg.destroy()
            self.refresh_menu()

        def _on_wt_push_claude(self, wt, repo):
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            r = {
                "path": wt["path"],
                "name": wt.get("branch", os.path.basename(wt["path"])),
                "branch": wt.get("branch", ""),
                "unpushed": 0,
            }
            dlg = CommitAllDialog(
                parent, [r],
                cfg.get("commit_ram_mb", 2048),
                cfg.get("commit_max_workers", 0),
                cfg.get("commit_timeout", 900),
                cfg.get("commit_budget_usd", 10.0),
                verb="Push", verb_ing="Pushing", verb_past="Pushed",
                worker=push_claude_repo,
                row_suffix=lambda rr: rr.get("branch", ""))
            dlg.run()
            dlg.destroy()
            self.refresh_menu()

        def _on_help(self):
            parent = self.window if (self.window and self.window.get_visible()) else None
            dlg = HelpDialog(parent)
            dlg.run()
            dlg.destroy()

        def _repo_item(self, r, unpushed=False):
            if unpushed:
                # In the unpushed section, lead with the unpushed-commit count
                # (a clean-but-unpushed repo has no dirty files to show).
                label = f"{r['name']}  ({r['branch']}, +{r['unpushed']})"
            else:
                track = ""
                if r["ahead"] or r["behind"]:
                    track = f" ↑{r['ahead']}↓{r['behind']}"
                label = f"{r['name']}  ({r['branch']}{track}, {r['count']})"
            item = Gtk.MenuItem(label=label)
            sub = Gtk.Menu()
            path = r["path"]
            self._action(sub, "Open terminal",
                         lambda *_: notify(self.window, *open_terminal(path)))
            self._action(sub, "Open Claude Code",
                         lambda *_: notify(self.window, *open_claude(path)))
            commit_label = "git commit" + (f" ({r['count']})" if r["count"] else "")
            self._action(sub, commit_label,
                         lambda *_: notify(self.window, *open_commit(path)))
            if r["count"]:
                self._action(sub, "Commit via Claude Code…",
                             lambda *_, r=r: self._on_commit_repo(r))
            push_label = "git push" + (f" (+{r['ahead']})" if r["ahead"] else "")
            self._action(sub, push_label,
                         lambda *_: notify(self.window, *open_push(path)))
            if r.get("has_remote") and r.get("unpushed", 0) > 0:
                self._action(sub, "Push via Claude Code…",
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
            else:
                behind_s = f"  ↓{wt['behind']}" if wt["behind"] else ""
                label = f"  {wt['branch']}  {age_str} ago{behind_s}"
            item = Gtk.MenuItem(label=label)
            sub = Gtk.Menu()
            path = wt["path"]
            repo_path = r["path"]

            self._action(sub, "Open terminal",
                         lambda *_, p=path: notify(self.window, *open_terminal(p)))
            self._action(sub, "Open Claude Code",
                         lambda *_, p=path: notify(self.window, *open_claude(p)))

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
                    self._action(sub, "Push via Claude Code…",
                                 lambda *_, w=wt, rr=r: self._on_wt_push_claude(w, rr))
                sub.append(Gtk.SeparatorMenuItem())
                self._action(sub, "Finish & merge via Claude Code…",
                             lambda *_, w=wt, rr=r: self._on_wt_finish(w, rr))
            else:
                sub.append(Gtk.SeparatorMenuItem())
                self._action(sub, "Close via Claude Code…",
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
            notify(self.window, *open_wt_claude(wt["path"], prompt))

        def _on_wt_finish(self, wt, r):
            cfg = self.config
            tmpl = cfg.get("worktree_stuck_finish_prompt") or STUCK_FINISH_PROMPT
            prompt = tmpl.format(path=wt["path"], branch=wt["branch"],
                                 repo_path=r["path"])
            notify(self.window, *open_wt_claude(wt["path"], prompt))

        def _on_wt_remove(self, wt, repo_path):
            ok, msg = remove_worktree(repo_path, wt["path"])
            notify(self.window, ok, msg or f"Removed {wt['branch']}")
            if ok:
                self.refresh_menu()

        def _stale_repo_item(self, r, severity):
            wt_list = r["stale_worktrees"][severity]
            oldest = max(wt_list, key=lambda w: w["last_commit_age_hours"])
            n = len(wt_list)
            age_str = _format_age(oldest["last_commit_age_hours"])
            if severity == "stuck":
                label = f"⚠ {r['name']}  ({n} stuck, oldest {age_str})"
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
            clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clip.set_text(text, -1)
            clip.store()

        # -- dashboard tier --
        def show_dashboard(self):
            if self.window is None:
                self.window = DashboardWindow(self, self.config)
            self.window.show_all()  # show the window chrome + containers
            self.window.present()
            self.window.reload()

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
                track = f"  ↑{git.get('ahead', 0)}↓{git.get('behind', 0)}"
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
            box.pack_start(self._btn("system-run", "Claude Code",
                                     lambda *_: notify(self, *open_claude(path))),
                           False, False, 0)
            push_tip = "git push" + (f" (↑{git.get('ahead')})" if git.get("ahead") else "")
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

        High-level: a progress bar plus one ✓/✗ row per repo. Detail (the raw
        git output) lives in an expander shown open by default. Pushing starts on
        open; the Close button stays disabled until every repo is done so the
        dialog can't be dismissed mid-run.
        """

        PENDING, RUNNING, OK, FAIL = "·", "↻", "✓", "✗"

        def __init__(self, parent, repos):
            super().__init__(title="Push all", transient_for=parent, modal=True)
            self._repos = repos
            self._marks = {}  # path -> status Gtk.Label
            self._done = False
            self.add_button("Close", Gtk.ResponseType.CLOSE)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, False)
            # Block the window-manager close button until the run finishes, so
            # the worker never updates widgets on a destroyed dialog.
            self.connect("delete-event", lambda *_: not self._done)
            self.set_default_size(460, 360)

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
            expander.set_expanded(True)  # show output by default
            area.pack_start(expander, False, False, 0)

            self.show_all()
            self._start()

        def _start(self):
            def work():
                ok = 0
                total = len(self._repos)
                for i, r in enumerate(self._repos, 1):
                    GLib.idle_add(self._mark, r["path"], self.RUNNING)
                    success, output = push_repo(r["path"])
                    ok += 1 if success else 0
                    GLib.idle_add(self._step, i, total, r, success, output)
                GLib.idle_add(self._finish, ok, total)
            threading.Thread(target=work, daemon=True).start()

        def _mark(self, path, glyph):
            self._marks[path].set_text(glyph)
            return False

        def _step(self, i, total, r, success, output):
            self._marks[r["path"]].set_text(self.OK if success else self.FAIL)
            self._bar.set_fraction(i / total)
            self._bar.set_text(f"{i} / {total}")
            buf = self._log.get_buffer()
            buf.insert(buf.get_end_iter(),
                       f"=== {r['name']} ===\n{output or '(no output)'}\n\n")
            return False

        def _finish(self, ok, total):
            failed = total - ok
            msg = f"Pushed {ok}/{total}"
            if failed:
                msg += f" · {failed} failed (see Details)"
            self._summary.set_text(msg)
            self._bar.set_fraction(1.0)
            self._done = True
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, True)
            return False

    class CommitAllDialog(Gtk.Dialog):
        """Modal progress window for committing every dirty repo via Claude Code.

        Like PushAllDialog, but the work is *bounded-parallel*: a
        ThreadPoolExecutor runs at most ``commit_workers(...)`` claude processes
        at once (RAM-derived) so the host isn't overloaded. Each repo's row goes
        ·→↻→✓/✗; the progress bar tracks a completed counter (not a loop index,
        since all futures are submitted at once but only W run). Close stays
        disabled until every repo is done.
        """

        PENDING, RUNNING, OK, FAIL = "·", "↻", "✓", "✗"

        def __init__(self, parent, repos, ram_mb, cap, timeout, budget_usd,
                     verb="Commit", verb_ing="Committing", verb_past="Committed",
                     worker=None, row_suffix=None):
            title = f"{verb} {repos[0]['name']}" if len(repos) == 1 else f"{verb} all"
            super().__init__(title=title, transient_for=parent, modal=True)
            self._repos = repos
            self._timeout = timeout
            self._budget = budget_usd
            self._verb_past = verb_past
            self._worker = worker if worker is not None else commit_repo
            self._row_suffix = row_suffix
            self._workers = commit_workers(ram_mb, cap)
            self._marks = {}  # path → status Gtk.Label
            self._done = False
            self.add_button("Close", Gtk.ResponseType.CLOSE)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, False)
            self.connect("delete-event", lambda *_: not self._done)
            self.set_default_size(480, 380)

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
                    if r["ahead"] or r["behind"]:
                        track = f" ↑{r['ahead']}↓{r['behind']}"
                    row_label = f"{r['name']}  ({r['branch']}{track}, {r['count']})"
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
            expander.set_expanded(True)  # show output by default
            area.pack_start(expander, False, False, 0)

            self.show_all()
            self._start()

        def _start(self):
            from concurrent.futures import ThreadPoolExecutor

            total = len(self._repos)

            def one(r):
                # Mark RUNNING here (inside the worker) so only genuinely-started
                # repos flip from PENDING — futures are all submitted at once but
                # only self._workers run concurrently.
                GLib.idle_add(self._mark, r["path"], self.RUNNING)
                return r, self._worker(r["path"], self._timeout, self._budget)

            def work():
                ok = 0
                done = 0
                # finally: always re-enable Close — if the loop ever raised, the
                # delete-event guard would otherwise leave the modal unclosable.
                try:
                    with ThreadPoolExecutor(max_workers=self._workers) as ex:
                        from concurrent.futures import as_completed
                        futures = [ex.submit(one, r) for r in self._repos]
                        for fut in as_completed(futures):
                            r, (success, output) = fut.result()
                            ok += 1 if success else 0
                            done += 1
                            GLib.idle_add(self._step, done, total, r,
                                          success, output)
                finally:
                    GLib.idle_add(self._finish, ok, total)
            threading.Thread(target=work, daemon=True).start()

        def _mark(self, path, glyph):
            self._marks[path].set_text(glyph)
            return False

        def _step(self, done, total, r, success, output):
            self._marks[r["path"]].set_text(self.OK if success else self.FAIL)
            self._bar.set_fraction(done / total)
            self._bar.set_text(f"{done} / {total}")
            buf = self._log.get_buffer()
            buf.insert(buf.get_end_iter(),
                       f"=== {r['name']} ===\n{output or '(no output)'}\n\n")
            return False

        def _finish(self, ok, total):
            failed = total - ok
            msg = f"{self._verb_past} {ok}/{total}"
            if failed:
                msg += f" · {failed} failed"
            msg += " · see menu for final state"
            self._summary.set_text(msg)
            self._bar.set_fraction(1.0)
            self._done = True
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, True)
            return False

    class ConfigDialog(Gtk.Dialog):
        def __init__(self, parent, config):
            super().__init__(title="Settings", transient_for=parent, modal=True)
            self.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                             "Save", Gtk.ResponseType.OK)
            self._config = dict(config)
            self._config["excluded_repos"] = list(
                config.get("excluded_repos", []))
            self._repo_checks = {}  # path -> Gtk.CheckButton

            notebook = Gtk.Notebook()
            notebook.append_page(self._build_general_tab(),
                                 Gtk.Label(label="General"))
            notebook.append_page(self._build_git_tab(),
                                 Gtk.Label(label="Git"))
            notebook.append_page(self._build_repos_tab(),
                                 Gtk.Label(label="Repositories"))
            notebook.append_page(self._build_claude_tab(),
                                 Gtk.Label(label="Claude Code"))
            self.get_content_area().pack_start(notebook, True, True, 0)
            self.set_default_size(520, 520)
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

        def _build_claude_tab(self):
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            vbox.set_border_width(12)

            def section(title):
                lbl = Gtk.Label(xalign=0.0)
                # markup_escape_text is mandatory: set_markup silently renders an
                # empty label on invalid Pango XML (e.g. a bare & in a title string).
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

            # ── Commit ─────────────────────────────────────────────────────
            section("Commit via Claude Code")
            self._spin_commit_ram = spin_row(
                "RAM/proc (MB):", "commit_ram_mb", 2048, 256, 65536, 256,
                hint="RAM budgeted per claude process; workers = MemAvailable ÷ this")
            self._spin_commit_workers = spin_row(
                "Max workers:", "commit_max_workers", 0, 0, 64, 1,
                hint="0 = auto (RAM- and CPU-derived); >0 caps concurrency")
            self._spin_commit_timeout = spin_row(
                "Timeout (s):", "commit_timeout", 900, 30, 7200, 30,
                hint="per-repo cap before a claude run is killed")
            self._spin_commit_budget = spin_row(
                "Budget ($/repo):", "commit_budget_usd", 10.0, 0, 1000, 1,
                digits=2, hint="max claude spend per repo (0 = unbounded)")

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
            ("item", "Open Claude Code",
             "Opens Claude Code interactively in a terminal."),
            ("item", "git commit (N)",
             "Opens a terminal with your editor so you can write the commit "
             "message yourself. Good for quick, focused commits."),
            ("item", "Commit via Claude Code…",
             "Claude Code inspects the diff, groups changes into logical commits "
             "with appropriate messages, fixes any pre-commit hook failures, then "
             "optionally merges the branch into main. A progress dialog shows "
             "per-repo status."),
            ("h2", "Unpushed repos"),
            ("p", "Repos with local commits not yet on a remote appear in the "
                  "Unpushed section."),
            ("item", "git push",
             "Opens a terminal and runs git push. Use this when you need to "
             "enter a passphrase or watch the output interactively."),
            ("item", "Push via Claude Code…",
             "Claude Code runs git push, handles non-fast-forward divergence "
             "(pull --rebase + retry), and fixes pre-push hook failures. Use "
             "when a plain push fails and you want errors fixed automatically."),
            ("h2", "Stale worktrees"),
            ("p", "Extra git worktrees (from git worktree add) that have gone "
                  "quiet appear as ⚠ Stuck or ⏸ Idle sections."),
            ("item", "⚠ Stuck",
             "A worktree with uncommitted changes sitting idle longer than the "
             "configured threshold. Use “Finish & merge via Claude Code” "
             "to commit, merge into main, and remove the worktree automatically."),
            ("item", "⏸ Idle",
             "A clean worktree with no ahead commits sitting idle. Use "
             "“Close via Claude Code” to review and remove it, or "
             "“Remove worktree” for an immediate direct delete."),
            ("h2", "Dashboard"),
            ("p", "Lists every repo with full status. Open with "
                  "“Show dashboard…” or by re-launching the tray. "
                  "Each row has buttons for Terminal, Claude Code, Push, GitHub, "
                  "and Open folder."),
            ("h2", "Settings"),
            ("item", "General",
             "Scan root directory, depth, refresh interval, terminal."),
            ("item", "Git",
             "Show/hide remoteless repos; stale-worktree thresholds."),
            ("item", "Repositories",
             "Per-repo include/exclude list. Rescan after changing the root."),
            ("item", "Claude Code",
             "RAM/worker/timeout/budget limits for headless Claude runs; "
             "customisable prompts for worktree close and finish actions. "
             "Placeholders {path}, {branch}, {repo_path} are substituted at "
             "runtime."),
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

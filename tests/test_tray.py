#!/usr/bin/env python3
"""Unit tests for the tray app's pure helpers (no GTK / no `gi` needed).

The GUI layer keeps all ``gi`` imports inside ``run_gui()``, so the module
imports cleanly here and we can test the data/action helpers in isolation.
Git-backed tests skip when git is unavailable; everything else always runs.
"""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TRAY_PY = os.path.join(ROOT, "tray", "repodash_tray.py")
HAVE_GIT = shutil.which("git") is not None


def _load_tray():
    spec = importlib.util.spec_from_file_location("repodash_tray", TRAY_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tray = _load_tray()


def _init_repo(path, origin=None):
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "t"], check=True)
    if origin:
        subprocess.run(["git", "-C", path, "remote", "add", "origin", origin],
                       check=True)


def _commit(repo, fname="f.txt", text="x"):
    with open(os.path.join(repo, fname), "w") as f:
        f.write(text)
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "c"], check=True)


def _clone_of_bare(d):
    """A working clone of a fresh local bare remote, with git identity set."""
    remote = os.path.join(d, "remote.git")
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    work = os.path.join(d, "work")
    subprocess.run(["git", "clone", "-q", remote, work], check=True)
    subprocess.run(["git", "-C", work, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", work, "config", "user.name", "t"], check=True)
    return work


class GithubUrlTest(unittest.TestCase):
    def test_ssh_scp_form(self):
        self.assertEqual(
            tray.normalize_github_url("git@github.com:owner/repo.git"),
            "https://github.com/owner/repo")

    def test_ssh_url_form(self):
        self.assertEqual(
            tray.normalize_github_url("ssh://git@github.com/owner/repo.git"),
            "https://github.com/owner/repo")

    def test_https_with_and_without_git_suffix(self):
        self.assertEqual(
            tray.normalize_github_url("https://github.com/owner/repo.git"),
            "https://github.com/owner/repo")
        self.assertEqual(
            tray.normalize_github_url("https://github.com/owner/repo"),
            "https://github.com/owner/repo")

    def test_non_github_and_empty_return_none(self):
        self.assertIsNone(tray.normalize_github_url("git@gitlab.com:o/r.git"))
        self.assertIsNone(tray.normalize_github_url("https://example.com/x.git"))
        self.assertIsNone(tray.normalize_github_url(""))
        self.assertIsNone(tray.normalize_github_url(None))

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_github_url_reads_origin(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo, origin="git@github.com:acme/widget.git")
            self.assertEqual(tray.github_url(repo),
                             "https://github.com/acme/widget")

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_github_url_none_without_remote(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo)
            self.assertIsNone(tray.github_url(repo))


class TerminalArgvTest(unittest.TestCase):
    def test_ptyxis(self):
        self.assertEqual(tray.terminal_argv("ptyxis", "/x"),
                         ["ptyxis", "--new-window", "-d", "/x"])
        self.assertEqual(
            tray.terminal_argv("ptyxis", "/x", "claude"),
            ["ptyxis", "--new-window", "-d", "/x", "--",
             "bash", "-lc", "claude; exec bash"])

    def test_gnome_terminal(self):
        self.assertEqual(tray.terminal_argv("gnome-terminal", "/x"),
                         ["gnome-terminal", "--working-directory=/x"])
        argv = tray.terminal_argv("gnome-terminal", "/x", "claude")
        self.assertEqual(argv[:2], ["gnome-terminal", "--working-directory=/x"])
        self.assertIn("claude; exec bash", argv)

    def test_kgx_and_ghostty_and_xterm(self):
        self.assertEqual(tray.terminal_argv("kgx", "/x")[:1], ["kgx"])
        self.assertIn("claude; exec bash",
                      " ".join(tray.terminal_argv("kgx", "/x", "claude")))
        self.assertEqual(tray.terminal_argv("ghostty", "/x"),
                         ["ghostty", "--working-directory=/x"])
        self.assertEqual(tray.terminal_argv("xterm", "/x"), ["xterm"])

    def test_absolute_path_uses_basename_dialect(self):
        self.assertEqual(
            tray.terminal_argv("/usr/bin/ptyxis", "/x"),
            ["/usr/bin/ptyxis", "--new-window", "-d", "/x"])

    def test_unknown_terminal_raises(self):
        with self.assertRaises(ValueError):
            tray.terminal_argv("nonsuch", "/x")


class DetectTerminalTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("REPODASH_TERMINAL")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("REPODASH_TERMINAL", None)
        else:
            os.environ["REPODASH_TERMINAL"] = self._saved

    def test_override_present_on_path(self):
        os.environ["REPODASH_TERMINAL"] = "sh"  # always on PATH
        self.assertEqual(tray.detect_terminal(), "sh")

    def test_override_missing_returns_none(self):
        os.environ["REPODASH_TERMINAL"] = "definitely-not-a-real-terminal-xyz"
        self.assertIsNone(tray.detect_terminal())


class PushActionTest(unittest.TestCase):
    def test_open_push_uses_git_push_in_terminal(self):
        # With a forced terminal, open_push should build a keep-open `git push`.
        saved = os.environ.get("REPODASH_TERMINAL")
        os.environ["REPODASH_TERMINAL"] = "ptyxis"
        try:
            argv = tray.terminal_argv("ptyxis", "/x", "git push")
            self.assertIn("git push; exec bash", argv)
            argv = tray.terminal_argv("ptyxis", "/x", "git add -A && git commit")
            self.assertIn("git add -A && git commit; exec bash", argv)
        finally:
            if saved is None:
                os.environ.pop("REPODASH_TERMINAL", None)
            else:
                os.environ["REPODASH_TERMINAL"] = saved


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("HOME")
        self._xdg = os.environ.get("XDG_CONFIG_HOME")
        self._tmp = tempfile.mkdtemp(prefix="repodash-autostart-")
        os.environ["XDG_CONFIG_HOME"] = self._tmp

    def tearDown(self):
        if self._xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._xdg
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_enable_then_disable(self):
        self.assertFalse(tray.autostart_enabled())
        self.assertTrue(tray.set_autostart(True))
        self.assertTrue(tray.autostart_enabled())
        self.assertTrue(os.path.isfile(tray.autostart_file()))

        with open(tray.autostart_file()) as f:
            content = f.read()
        self.assertIn("repodash_tray.py", content)
        self.assertIn("X-GNOME-Autostart-enabled=true", content)

        self.assertFalse(tray.set_autostart(False))
        self.assertFalse(tray.autostart_enabled())

    def test_disable_when_absent_is_noop(self):
        self.assertFalse(tray.set_autostart(False))  # no exception


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._xdg = os.environ.get("XDG_CONFIG_HOME")
        self._tmp = tempfile.mkdtemp(prefix="repodash-config-")
        os.environ["XDG_CONFIG_HOME"] = self._tmp

    def tearDown(self):
        if self._xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._xdg
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_load_defaults_when_missing(self):
        cfg = tray.load_config()
        self.assertEqual(cfg["base_dir"], "")
        self.assertEqual(cfg["depth"], 0)
        self.assertEqual(cfg["refresh_interval"], 0)
        self.assertEqual(cfg["excluded_repos"], [])
        self.assertEqual(cfg["terminal"], "")
        self.assertTrue(cfg["show_remoteless"])

    def test_roundtrip_save_load(self):
        cfg = tray.load_config()
        cfg["base_dir"] = "/some/path"
        cfg["depth"] = 4
        cfg["refresh_interval"] = 120
        cfg["terminal"] = "xterm"
        cfg["show_remoteless"] = False
        tray.save_config(cfg)
        loaded = tray.load_config()
        self.assertEqual(loaded["base_dir"], "/some/path")
        self.assertEqual(loaded["depth"], 4)
        self.assertEqual(loaded["refresh_interval"], 120)
        self.assertEqual(loaded["terminal"], "xterm")
        self.assertFalse(loaded["show_remoteless"])

    def test_excluded_repos_survives_roundtrip(self):
        cfg = tray.load_config()
        cfg["excluded_repos"] = ["/repo/a", "/repo/b"]
        tray.save_config(cfg)
        loaded = tray.load_config()
        self.assertEqual(sorted(loaded["excluded_repos"]), ["/repo/a", "/repo/b"])

    def test_corrupt_config_returns_defaults(self):
        path = tray.config_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{not valid json")
        cfg = tray.load_config()
        self.assertEqual(cfg["depth"], 0)

    def test_resolve_depth_uses_env_when_zero(self):
        saved = os.environ.get("REPODASH_DEPTH")
        os.environ["REPODASH_DEPTH"] = "7"
        try:
            self.assertEqual(tray.resolve_depth({"depth": 0}), 7)
            self.assertEqual(tray.resolve_depth({"depth": 5}), 5)
        finally:
            if saved is None:
                os.environ.pop("REPODASH_DEPTH", None)
            else:
                os.environ["REPODASH_DEPTH"] = saved

    def test_resolve_interval_uses_env_when_zero(self):
        saved = os.environ.get("REPODASH_TRAY_INTERVAL")
        os.environ["REPODASH_TRAY_INTERVAL"] = "60"
        try:
            self.assertEqual(tray.resolve_interval({"refresh_interval": 0}), 60)
            self.assertEqual(tray.resolve_interval({"refresh_interval": 300}), 300)
        finally:
            if saved is None:
                os.environ.pop("REPODASH_TRAY_INTERVAL", None)
            else:
                os.environ["REPODASH_TRAY_INTERVAL"] = saved

    def test_resolve_base_dir_uses_env_when_empty(self):
        saved = os.environ.get("REPODASH_DIR")
        os.environ["REPODASH_DIR"] = "/env/repos"
        try:
            self.assertEqual(tray.resolve_base_dir({"base_dir": ""}), "/env/repos")
            self.assertEqual(tray.resolve_base_dir({"base_dir": "/cfg/repos"}),
                             "/cfg/repos")
        finally:
            if saved is None:
                os.environ.pop("REPODASH_DIR", None)
            else:
                os.environ["REPODASH_DIR"] = saved

    def _save_env(self, *keys):
        return {k: os.environ.get(k) for k in keys}

    def _restore_env(self, saved):
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_apply_config_sets_terminal(self):
        saved = self._save_env("REPODASH_TERMINAL")
        os.environ.pop("REPODASH_TERMINAL", None)
        try:
            tray.apply_config_to_env({"terminal": "xterm", "base_dir": "",
                                      "depth": 0, "refresh_interval": 0})
            self.assertEqual(os.environ.get("REPODASH_TERMINAL"), "xterm")
        finally:
            self._restore_env(saved)

    def test_apply_config_clears_terminal_when_blank(self):
        saved = self._save_env("REPODASH_TERMINAL")
        os.environ["REPODASH_TERMINAL"] = "ghostty"
        try:
            tray.apply_config_to_env({"terminal": "", "base_dir": "",
                                      "depth": 0, "refresh_interval": 0})
            self.assertNotIn("REPODASH_TERMINAL", os.environ)
        finally:
            self._restore_env(saved)

    def test_apply_config_sets_base_dir_and_depth(self):
        saved = self._save_env("REPODASH_DIR", "REPODASH_DEPTH")
        try:
            tray.apply_config_to_env({"base_dir": "/tmp/repos", "depth": 4,
                                      "refresh_interval": 0, "terminal": ""})
            self.assertEqual(os.environ.get("REPODASH_DIR"), "/tmp/repos")
            self.assertEqual(os.environ.get("REPODASH_DEPTH"), "4")
        finally:
            self._restore_env(saved)

    def test_apply_config_zero_depth_leaves_env_untouched(self):
        saved = self._save_env("REPODASH_DEPTH")
        os.environ["REPODASH_DEPTH"] = "5"
        try:
            tray.apply_config_to_env({"base_dir": "", "depth": 0,
                                      "refresh_interval": 0, "terminal": ""})
            self.assertEqual(os.environ.get("REPODASH_DEPTH"), "5")
        finally:
            self._restore_env(saved)


class DiscoveryTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_find_repos_and_dirty_state(self):
        with tempfile.TemporaryDirectory() as base:
            clean = os.path.join(base, "clean")
            dirty = os.path.join(base, "dirty")
            _init_repo(clean)
            _init_repo(dirty)
            # leave `clean` empty (no changes), make `dirty` dirty
            with open(os.path.join(dirty, "new.txt"), "w") as f:
                f.write("hi")

            repos = tray.find_repos(base, depth=2)
            self.assertEqual({os.path.basename(r) for r in repos},
                             {"clean", "dirty"})

            states = {s["name"]: s for s in tray.scan_dirty(base, depth=2)}
            self.assertFalse(states["clean"]["dirty"])
            self.assertTrue(states["dirty"]["dirty"])
            self.assertEqual(states["dirty"]["count"], 1)

    def test_find_repos_missing_base_is_empty(self):
        self.assertEqual(tray.find_repos("/no/such/path/xyz", depth=2), [])


class UnpushedTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_committed_but_never_pushed_is_unpushed(self):
        # A remote is configured but the branch was never pushed, so it has no
        # upstream: `ahead` stays 0 while `unpushed` catches the local commit.
        with tempfile.TemporaryDirectory() as d:
            work = _clone_of_bare(d)
            _commit(work)
            st = tray.git_status(work)
            self.assertTrue(st["has_remote"])
            self.assertEqual(st["ahead"], 0)
            self.assertGreaterEqual(st["unpushed"], 1)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_fully_pushed_is_not_unpushed(self):
        with tempfile.TemporaryDirectory() as d:
            work = _clone_of_bare(d)
            _commit(work)
            subprocess.run(["git", "-C", work, "push", "-q", "origin", "HEAD"],
                           check=True)
            st = tray.git_status(work)
            self.assertTrue(st["has_remote"])
            self.assertEqual(st["unpushed"], 0)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_other_branches_do_not_inflate_count(self):
        # Unpushed is scoped to the current branch (HEAD), not all local
        # branches: a fully-pushed main must report 0 even when an unpushed
        # feature branch carries its own commit.
        with tempfile.TemporaryDirectory() as d:
            work = _clone_of_bare(d)
            _commit(work)
            subprocess.run(["git", "-C", work, "push", "-q", "origin", "HEAD"],
                           check=True)
            subprocess.run(["git", "-C", work, "checkout", "-q", "-b", "feature"],
                           check=True)
            _commit(work, fname="g.txt")
            subprocess.run(["git", "-C", work, "checkout", "-q", "-"], check=True)
            st = tray.git_status(work)
            self.assertEqual(st["unpushed"], 0)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_no_remote_is_never_unpushed(self):
        # Guard against the `--not --remotes` trap: with no remote to exclude,
        # rev-list would otherwise count every commit in the repo.
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "local")
            _init_repo(repo)
            _commit(repo)
            st = tray.git_status(repo)
            self.assertFalse(st["has_remote"])
            self.assertEqual(st["unpushed"], 0)


class PushRepoTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_first_push_sets_upstream(self):
        # No upstream on the branch (clone of an empty remote) → push_repo must
        # use `-u <remote> HEAD` so the first push actually goes through.
        with tempfile.TemporaryDirectory() as d:
            work = _clone_of_bare(d)
            _commit(work)
            self.assertGreaterEqual(tray.git_status(work)["unpushed"], 1)
            ok, out = tray.push_repo(work)
            self.assertTrue(ok, out)
            self.assertEqual(tray.git_status(work)["unpushed"], 0)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_subsequent_push_with_upstream(self):
        with tempfile.TemporaryDirectory() as d:
            work = _clone_of_bare(d)
            _commit(work)
            ok, _ = tray.push_repo(work)          # first push sets the upstream
            self.assertTrue(ok)
            _commit(work, fname="g.txt")
            self.assertEqual(tray.git_status(work)["unpushed"], 1)
            ok, out = tray.push_repo(work)         # now a plain `git push`
            self.assertTrue(ok, out)
            self.assertEqual(tray.git_status(work)["unpushed"], 0)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_failure_reports_output_without_hanging(self):
        # A file:// remote that does not exist fails fast (no auth prompt).
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo, origin="file:///nonexistent/repo.git")
            _commit(repo)
            ok, out = tray.push_repo(repo)
            self.assertFalse(ok)
            self.assertTrue(out)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_no_remote_reports_clearly(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo)
            _commit(repo)
            ok, out = tray.push_repo(repo)
            self.assertFalse(ok)
            self.assertIn("no remote", out)


class CommitWorkersTest(unittest.TestCase):
    """commit_workers() concurrency math (RAM ÷ budget, clamped)."""

    def setUp(self):
        self._orig = tray._mem_available_mb

    def tearDown(self):
        tray._mem_available_mb = self._orig

    def _fake_mem(self, mb):
        tray._mem_available_mb = lambda: mb

    def test_ram_divides_budget(self):
        self._fake_mem(16384)            # 16 GB available
        # 16384 // 2048 = 8, but clamped by CPU count.
        expected = min(8, os.cpu_count() or 1)
        self.assertEqual(tray.commit_workers(2048, 0), expected)

    def test_tiny_ram_clamps_to_one(self):
        self._fake_mem(512)              # less than one 2 GB slot
        self.assertEqual(tray.commit_workers(2048, 0), 1)

    def test_unknown_ram_is_one(self):
        self._fake_mem(0)                # /proc/meminfo unreadable
        self.assertEqual(tray.commit_workers(2048, 0), 1)

    def test_cap_limits_workers(self):
        self._fake_mem(65536)            # plenty of RAM
        self.assertEqual(tray.commit_workers(1024, 2), 2)

    def test_cap_zero_is_auto(self):
        self._fake_mem(65536)
        # No cap → bounded only by CPU count (RAM allows many).
        self.assertEqual(tray.commit_workers(256, 0), os.cpu_count() or 1)

    def test_always_at_least_one(self):
        self._fake_mem(0)
        self.assertGreaterEqual(tray.commit_workers(99999, 99), 1)


class MemAvailableTest(unittest.TestCase):
    @unittest.skipUnless(os.path.exists("/proc/meminfo"), "no /proc/meminfo")
    def test_returns_positive_on_linux(self):
        self.assertGreater(tray._mem_available_mb(), 0)


class CommitArgvTest(unittest.TestCase):
    def test_contains_headless_flags(self):
        argv = tray.commit_argv("/x/claude", 10.0)
        self.assertEqual(argv[0], "/x/claude")
        self.assertIn("-p", argv)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--max-budget-usd", argv)
        self.assertIn("10.0", argv)
        # The prompt is passed as the argument to -p.
        self.assertEqual(argv[argv.index("-p") + 1], tray.COMMIT_PROMPT)

    def test_zero_budget_omits_flag(self):
        argv = tray.commit_argv("/x/claude", 0)
        self.assertNotIn("--max-budget-usd", argv)


class CommitRepoTest(unittest.TestCase):
    def test_missing_claude_reports_clearly(self):
        orig = tray.shutil.which
        tray.shutil.which = lambda _: None
        try:
            ok, out = tray.commit_repo("/tmp", timeout=5)
        finally:
            tray.shutil.which = orig
        self.assertFalse(ok)
        self.assertIn("claude not found", out)


class PushClaudeArgvTest(unittest.TestCase):
    def test_contains_headless_flags(self):
        argv = tray.push_claude_argv("/x/claude", 10.0)
        self.assertEqual(argv[0], "/x/claude")
        self.assertIn("-p", argv)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--max-budget-usd", argv)
        self.assertIn("10.0", argv)
        self.assertEqual(argv[argv.index("-p") + 1], tray.PUSH_PROMPT)

    def test_zero_budget_omits_flag(self):
        argv = tray.push_claude_argv("/x/claude", 0)
        self.assertNotIn("--max-budget-usd", argv)

    def test_prompt_differs_from_commit_prompt(self):
        self.assertNotEqual(tray.PUSH_PROMPT, tray.COMMIT_PROMPT)
        self.assertIn("git push", tray.PUSH_PROMPT)
        self.assertIn("pull --rebase", tray.PUSH_PROMPT)


class PushClaudeRepoTest(unittest.TestCase):
    def test_missing_claude_reports_clearly(self):
        orig = tray.shutil.which
        tray.shutil.which = lambda _: None
        try:
            ok, out = tray.push_claude_repo("/tmp", timeout=5)
        finally:
            tray.shutil.which = orig
        self.assertFalse(ok)
        self.assertIn("claude not found", out)


class StaleWorktreeTest(unittest.TestCase):

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_no_extra_worktrees_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo)
            _commit(repo)
            result = tray.scan_worktrees(repo, idle_hours=24, stuck_hours=12)
            self.assertEqual(result, {"stuck": [], "idle": []})

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_idle_worktree_detected_at_zero_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo)
            _commit(repo)
            wt = os.path.join(d, "wt")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat", wt],
                           check=True)
            result = tray.scan_worktrees(repo, idle_hours=0, stuck_hours=9999)
            branches = [e["branch"] for e in result["idle"]]
            self.assertIn("feat", branches)
            self.assertEqual(result["stuck"], [])

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_stuck_worktree_detected_at_zero_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo)
            _commit(repo)
            wt = os.path.join(d, "wt")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat", wt],
                           check=True)
            with open(os.path.join(wt, "new.txt"), "w") as f:
                f.write("change")
            result = tray.scan_worktrees(repo, idle_hours=9999, stuck_hours=0)
            branches = [e["branch"] for e in result["stuck"]]
            self.assertIn("feat", branches)
            self.assertEqual(result["idle"], [])

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_dirty_worktree_is_never_idle(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo)
            _commit(repo)
            wt = os.path.join(d, "wt")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat", wt],
                           check=True)
            with open(os.path.join(wt, "new.txt"), "w") as f:
                f.write("change")
            result = tray.scan_worktrees(repo, idle_hours=0, stuck_hours=0)
            self.assertEqual(result["idle"], [])
            self.assertEqual(len(result["stuck"]), 1)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_worktree_entry_has_required_fields(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo)
            _commit(repo)
            wt = os.path.join(d, "wt")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat", wt],
                           check=True)
            result = tray.scan_worktrees(repo, idle_hours=0, stuck_hours=9999)
            self.assertEqual(len(result["idle"]), 1)
            e = result["idle"][0]
            for field in ("path", "branch", "last_commit_age_hours", "behind", "dirty"):
                self.assertIn(field, e)
            self.assertEqual(e["branch"], "feat")
            self.assertFalse(e["dirty"])
            self.assertIsInstance(e["last_commit_age_hours"], float)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_high_threshold_suppresses_detection(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo)
            _commit(repo)
            wt = os.path.join(d, "wt")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat", wt],
                           check=True)
            result = tray.scan_worktrees(repo, idle_hours=9999, stuck_hours=9999)
            self.assertEqual(result, {"stuck": [], "idle": []})

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_scan_dirty_attaches_stale_worktrees_with_cfg(self):
        with tempfile.TemporaryDirectory() as base:
            repo = os.path.join(base, "myrepo")
            _init_repo(repo)
            _commit(repo)
            wt = os.path.join(base, "wt")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat", wt],
                           check=True)
            cfg = {"show_stale_worktrees": True,
                   "stale_worktree_idle_hours": 0,
                   "stale_worktree_stuck_hours": 9999}
            repos = tray.scan_dirty(base, depth=2, cfg=cfg)
            found = {r["name"]: r for r in repos}
            self.assertIn("myrepo", found)
            self.assertNotIn("wt", found)
            sw = found["myrepo"].get("stale_worktrees", {})
            self.assertIn("feat", [e["branch"] for e in sw.get("idle", [])])

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_scan_dirty_no_stale_when_disabled(self):
        with tempfile.TemporaryDirectory() as base:
            repo = os.path.join(base, "r")
            _init_repo(repo)
            _commit(repo)
            cfg = {"show_stale_worktrees": False,
                   "stale_worktree_idle_hours": 0,
                   "stale_worktree_stuck_hours": 0}
            repos = tray.scan_dirty(base, depth=2, cfg=cfg)
            for r in repos:
                self.assertNotIn("stale_worktrees", r)

    def test_format_age_hours(self):
        self.assertEqual(tray._format_age(3), "3h")
        self.assertEqual(tray._format_age(47), "47h")

    def test_format_age_days(self):
        self.assertEqual(tray._format_age(48), "2.0d")
        self.assertEqual(tray._format_age(72), "3.0d")


if __name__ == "__main__":
    unittest.main()

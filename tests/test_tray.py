#!/usr/bin/env python3
"""Unit tests for the tray app's pure helpers (no GTK / no `gi` needed).

The GUI layer keeps all ``gi`` imports inside ``run_gui()``, so the module
imports cleanly here and we can test the data/action helpers in isolation.
Git-backed tests skip when git is unavailable; everything else always runs.
"""
import contextlib
import importlib.util
import io
import json
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

    def test_ghostty_and_xterm_with_command(self):
        argv = tray.terminal_argv("ghostty", "/x", "claude")
        self.assertIn("claude; exec bash", argv)
        argv = tray.terminal_argv("xterm", "/x", "claude")
        self.assertIn("claude; exec bash", argv)

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

    def test_no_override_and_none_on_path_returns_none(self):
        os.environ.pop("REPODASH_TERMINAL", None)
        orig_which = tray.shutil.which
        tray.shutil.which = lambda _: None
        try:
            self.assertIsNone(tray.detect_terminal())
        finally:
            tray.shutil.which = orig_which


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


class ClipboardArgvTest(unittest.TestCase):
    def setUp(self):
        self._wayland = os.environ.get("WAYLAND_DISPLAY")

    def tearDown(self):
        if self._wayland is None:
            os.environ.pop("WAYLAND_DISPLAY", None)
        else:
            os.environ["WAYLAND_DISPLAY"] = self._wayland

    def test_wayland_uses_wl_copy(self):
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        self.assertEqual(tray.clipboard_argv(), ["wl-copy"])

    def test_no_wayland_uses_xclip(self):
        os.environ.pop("WAYLAND_DISPLAY", None)
        self.assertEqual(tray.clipboard_argv(),
                         ["xclip", "-selection", "clipboard"])


class CopyToClipboardTest(unittest.TestCase):
    """copy_to_clipboard() shells out to wl-copy/xclip rather than using
    Gtk.Clipboard. RCA (2026-07-03): 'Copy path' was the one tray action that
    never called notify(), and even after wiring notify() in, Gtk.Clipboard's
    set_text()/store() both return normally when the clipboard manager can't
    take selection ownership from an unfocused tray-menu context (the classic
    Wayland/XWayland tray-clipboard failure) — there's no exception to catch,
    so notify() alone can't surface it either. Shelling out to a dedicated
    clipboard tool (which forks and serves the selection independently of our
    GTK event loop) gives a real exit code to report through notify(), the
    same pattern open_folder/open_terminal already use for xdg-open/terminals.
    """

    def setUp(self):
        self._which = tray.shutil.which
        self._run = tray.subprocess.run

    def tearDown(self):
        tray.shutil.which = self._which
        tray.subprocess.run = self._run

    def test_missing_tool_reports_clearly(self):
        tray.shutil.which = lambda _: None
        ok, msg = tray.copy_to_clipboard("/home/user/repo")
        self.assertFalse(ok)
        self.assertIn("not found on PATH", msg)

    def test_success(self):
        tray.shutil.which = lambda _: "/usr/bin/tool"
        seen = {}

        def fake_run(argv, input=None, text=None, capture_output=None, timeout=None):
            seen["argv"] = argv
            seen["input"] = input

            class R:
                returncode = 0
                stderr = ""
            return R()

        tray.subprocess.run = fake_run
        ok, msg = tray.copy_to_clipboard("/home/user/repo")
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        self.assertEqual(seen["input"], "/home/user/repo")

    def test_nonzero_exit_reports_stderr(self):
        tray.shutil.which = lambda _: "/usr/bin/tool"

        def fake_run(argv, input=None, text=None, capture_output=None, timeout=None):
            class R:
                returncode = 1
                stderr = "no clipboard manager running\n"
            return R()

        tray.subprocess.run = fake_run
        ok, msg = tray.copy_to_clipboard("/x")
        self.assertFalse(ok)
        self.assertIn("no clipboard manager running", msg)

    def test_subprocess_error_is_caught(self):
        tray.shutil.which = lambda _: "/usr/bin/tool"

        def fake_run(*a, **k):
            raise OSError("boom")

        tray.subprocess.run = fake_run
        ok, msg = tray.copy_to_clipboard("/x")
        self.assertFalse(ok)
        self.assertIn("boom", msg)


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

    def test_model_and_effort_flags(self):
        argv = tray.commit_argv("/x/claude", 10.0, "opus", "high")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertIn("--effort", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_empty_model_and_effort_omit_flags(self):
        argv = tray.commit_argv("/x/claude", 10.0, "", "")
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)


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


class MarkupSafetyTest(unittest.TestCase):
    """Regression guard for the silent set_markup failure on unescaped Pango XML.

    RCA (2026-06-28): Settings → Claude Code showed the "Idle worktree" section
    header but not the "Stuck worktree" one.  Both headers were inserted in the
    correct DOM order, yet only the stuck one was invisible.  The difference: the
    stuck title string is "⚠  Stuck worktree — finish & merge prompt", which
    contains a bare &.  Pango markup is XML; & must be written as &amp;.
    Gtk.Label.set_markup() does NOT raise on invalid XML — it silently renders
    an empty label.  The idle title had no & so it rendered correctly.

    Fix: both section() helpers now wrap the title in
    GLib.markup_escape_text() before embedding it in the <b>…</b> markup
    string.

    Guardrail: the tests below fail if
      (a) markup_escape_text is removed from a section() helper, or
      (b) a new section() title containing & is added without the helper
          (the ampersand-title presence test proves the check is still live).
    """

    def _source(self):
        with open(TRAY_PY, encoding="utf-8") as f:
            return f.read()

    def test_section_helpers_escape_title(self):
        """Both section() definitions must use GLib.markup_escape_text(title)."""
        import re
        source = self._source()
        safe = 'set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")'
        unsafe = re.compile(r'set_markup\(\s*f"<b>\{(?!GLib\.markup_escape_text)')
        self.assertGreaterEqual(
            source.count(safe), 2,
            f"expected ≥2 escaped section() set_markup calls in {TRAY_PY}")
        bad = unsafe.findall(source)
        self.assertEqual(
            bad, [],
            f"unescaped set_markup interpolation found in {TRAY_PY}: {bad}")

    def test_ampersand_title_still_present(self):
        """At least one section() call must have & in its title string.

        This keeps the guardrail live: if every & title were renamed, the
        escape requirement would be impossible to trigger and the above test
        would no longer prove anything meaningful.
        """
        import re
        source = self._source()
        titles = re.findall(r'section\("([^"]+)"\)', source)
        amp = [t for t in titles if "&" in t]
        self.assertTrue(
            amp,
            f"no section() title with & found — guardrail may be stale. "
            f"Titles seen: {titles}")


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

    def test_model_and_effort_flags(self):
        argv = tray.push_claude_argv("/x/claude", 10.0, "opus", "high")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertIn("--effort", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

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
            self.assertEqual(result, {"stuck": [], "idle": [], "merged": []})

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_idle_worktree_detected_at_zero_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo)
            _commit(repo)
            wt = os.path.join(d, "wt")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat", wt],
                           check=True)
            _commit(wt, fname="wt.txt")  # unique commit keeps it in "idle", not "merged"
            result = tray.scan_worktrees(repo, idle_hours=0, stuck_hours=9999)
            branches = [e["branch"] for e in result["idle"]]
            self.assertIn("feat", branches)
            self.assertEqual(result["stuck"], [])
            self.assertEqual(result["merged"], [])

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
            _commit(wt, fname="wt.txt")  # unique commit keeps it in "idle", not "merged"
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
            self.assertEqual(result, {"stuck": [], "idle": [], "merged": []})

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_scan_dirty_attaches_stale_worktrees_with_cfg(self):
        with tempfile.TemporaryDirectory() as base:
            repo = os.path.join(base, "myrepo")
            _init_repo(repo)
            _commit(repo)
            wt = os.path.join(base, "wt")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat", wt],
                           check=True)
            _commit(wt, fname="wt.txt")  # unique commit keeps it in "idle", not "merged"
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
    def test_merged_worktree_detected_after_ff_merge(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            _init_repo(repo)
            _commit(repo)
            wt = os.path.join(d, "wt")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat", wt],
                           check=True)
            _commit(wt, fname="wt.txt")
            # Fast-forward merge feat into main so the branch has no unique commits left
            subprocess.run(["git", "-C", repo, "merge", "--ff-only", "feat"],
                           check=True)
            result = tray.scan_worktrees(repo, idle_hours=0, stuck_hours=9999)
            branches_merged = [e["branch"] for e in result["merged"]]
            self.assertIn("feat", branches_merged)
            self.assertEqual(result["idle"], [])
            self.assertEqual(result["stuck"], [])

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


class DepthIntervalEnvTest(unittest.TestCase):
    def test_scan_depth_invalid_env_falls_back(self):
        saved = os.environ.get("REPODASH_DEPTH")
        os.environ["REPODASH_DEPTH"] = "not-a-number"
        try:
            self.assertEqual(tray.scan_depth(), tray.DEFAULT_DEPTH)
        finally:
            if saved is None:
                os.environ.pop("REPODASH_DEPTH", None)
            else:
                os.environ["REPODASH_DEPTH"] = saved

    def test_refresh_interval_invalid_env_falls_back(self):
        saved = os.environ.get("REPODASH_TRAY_INTERVAL")
        os.environ["REPODASH_TRAY_INTERVAL"] = "nope"
        try:
            self.assertEqual(tray.refresh_interval(), tray.DEFAULT_INTERVAL)
        finally:
            if saved is None:
                os.environ.pop("REPODASH_TRAY_INTERVAL", None)
            else:
                os.environ["REPODASH_TRAY_INTERVAL"] = saved

    def test_apply_config_sets_interval(self):
        saved = os.environ.get("REPODASH_TRAY_INTERVAL")
        os.environ.pop("REPODASH_TRAY_INTERVAL", None)
        try:
            tray.apply_config_to_env({"base_dir": "", "depth": 0,
                                      "refresh_interval": 120, "terminal": ""})
            self.assertEqual(os.environ.get("REPODASH_TRAY_INTERVAL"), "120")
        finally:
            if saved is None:
                os.environ.pop("REPODASH_TRAY_INTERVAL", None)
            else:
                os.environ["REPODASH_TRAY_INTERVAL"] = saved


class SaveConfigErrorTest(unittest.TestCase):
    def test_unwritable_path_is_swallowed(self):
        orig = tray.config_file
        tmp = tempfile.mkdtemp(prefix="repodash-saveerr-")
        blocker = os.path.join(tmp, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        # A file where a directory needs to be created — makedirs must OSError.
        tray.config_file = lambda: os.path.join(blocker, "sub", "config.json")
        try:
            tray.save_config({"a": 1})  # must not raise
        finally:
            tray.config_file = orig
            shutil.rmtree(tmp, ignore_errors=True)


class GitHelperTest(unittest.TestCase):
    def test_git_exception_returns_empty_string(self):
        orig = tray.subprocess.run

        def raiser(*a, **k):
            raise OSError("boom")

        tray.subprocess.run = raiser
        try:
            self.assertEqual(tray._git("/tmp", "status"), "")
        finally:
            tray.subprocess.run = orig


class FindRepoDepthPruneTest(unittest.TestCase):
    def test_deep_nondir_repo_not_descended_past_depth(self):
        with tempfile.TemporaryDirectory() as base:
            deep = os.path.join(base, "a", "b", "c", "d")
            os.makedirs(deep, exist_ok=True)
            self.assertEqual(tray.find_repos(base, depth=1), [])


class ParseWorktreeListTest(unittest.TestCase):
    def test_detached_and_bare_and_normal_entries(self):
        raw = (
            "worktree /repo\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /repo/wt-detached\n"
            "detached\n"
            "\n"
            "worktree /repo/wt-bare\n"
            "bare\n"
            "\n"
            "worktree /repo/wt-feat\n"
            "branch refs/heads/feat\n"
        )
        entries = tray._parse_worktree_list(raw)
        by_path = {e["path"]: e for e in entries}
        self.assertEqual(by_path["/repo"]["branch"], "main")
        self.assertEqual(by_path["/repo/wt-detached"]["branch"], "(detached)")
        self.assertNotIn("/repo/wt-bare", by_path)  # bare worktrees excluded
        self.assertEqual(by_path["/repo/wt-feat"]["branch"], "feat")


class ScanWorktreesEarlyReturnTest(unittest.TestCase):
    def test_no_worktree_output_returns_empty_result(self):
        result = tray.scan_worktrees("/no/such/repo-xyz", 24, 12)
        self.assertEqual(result, {"stuck": [], "idle": [], "merged": []})

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_missing_worktree_dir_is_skipped(self):
        with tempfile.TemporaryDirectory() as base:
            repo = os.path.join(base, "repo")
            _init_repo(repo)
            _commit(repo)
            wt = os.path.join(base, "wt")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-q",
                           "-b", "feat", wt], check=True)
            shutil.rmtree(wt)  # removed on disk without telling git
            result = tray.scan_worktrees(repo, 0, 0)
            self.assertEqual(result, {"stuck": [], "idle": [], "merged": []})


class OpenActionsTest(unittest.TestCase):
    def setUp(self):
        self._detect = tray.detect_terminal
        self._targv = tray.terminal_argv
        self._popen = tray.subprocess.Popen
        self._which = tray.shutil.which
        self._open_terminal = tray.open_terminal
        self._github_url = tray.github_url
        self._open_url = tray.open_url

    def tearDown(self):
        tray.detect_terminal = self._detect
        tray.terminal_argv = self._targv
        tray.subprocess.Popen = self._popen
        tray.shutil.which = self._which
        tray.open_terminal = self._open_terminal
        tray.github_url = self._github_url
        tray.open_url = self._open_url

    def test_spawn_success(self):
        ok, msg = tray._spawn(["true"])
        self.assertTrue(ok)
        self.assertIsNone(msg)

    def test_spawn_failure(self):
        ok, msg = tray._spawn(["definitely-not-a-real-binary-xyz"])
        self.assertFalse(ok)
        self.assertTrue(msg)

    def test_open_terminal_no_terminal_found(self):
        tray.detect_terminal = lambda: None
        ok, msg = tray.open_terminal("/x")
        self.assertFalse(ok)
        self.assertIn("no terminal found", msg)

    def test_open_terminal_success(self):
        tray.detect_terminal = lambda: "ptyxis"
        tray.terminal_argv = lambda *a, **k: ["true"]
        ok, _ = tray.open_terminal(tempfile.gettempdir())
        self.assertTrue(ok)

    def test_open_claude_delegates_to_open_terminal(self):
        seen = {}

        def fake_open_terminal(path, command=None):
            seen["path"] = path
            seen["command"] = command
            return True, None

        tray.open_terminal = fake_open_terminal
        ok, _ = tray.open_claude("/x")
        self.assertTrue(ok)
        self.assertEqual(seen["command"], tray.CLAUDE_COMMAND)

    def test_open_url_no_url(self):
        ok, msg = tray.open_url("")
        self.assertFalse(ok)
        self.assertEqual(msg, "no URL")

    def test_open_url_spawns_xdg_open(self):
        seen = {}

        def fake_popen(argv, **kw):
            seen["argv"] = argv

            class P:
                pass
            return P()

        tray.subprocess.Popen = fake_popen
        ok, _ = tray.open_url("https://example.com")
        self.assertTrue(ok)
        self.assertEqual(seen["argv"], ["xdg-open", "https://example.com"])

    def test_open_github_no_remote(self):
        tray.github_url = lambda path: None
        ok, msg = tray.open_github("/x")
        self.assertFalse(ok)
        self.assertEqual(msg, "no GitHub remote")

    def test_open_github_opens_url(self):
        tray.github_url = lambda path: "https://github.com/x/y"
        seen = {}

        def fake_open_url(url):
            seen["url"] = url
            return True, None

        tray.open_url = fake_open_url
        ok, _ = tray.open_github("/x")
        self.assertTrue(ok)
        self.assertEqual(seen["url"], "https://github.com/x/y")

    def test_open_folder_calls_xdg_open(self):
        seen = {}

        def fake_popen(argv, **kw):
            seen["argv"] = argv

            class P:
                pass
            return P()

        tray.subprocess.Popen = fake_popen
        ok, _ = tray.open_folder("/x")
        self.assertTrue(ok)
        self.assertEqual(seen["argv"], ["xdg-open", "/x"])

    def test_open_commit_uses_add_and_commit(self):
        seen = {}

        def fake(path, command=None):
            seen["command"] = command
            return True, None

        tray.open_terminal = fake
        tray.open_commit("/x")
        self.assertEqual(seen["command"], "git add -A && git commit")

    def test_open_wt_claude_no_claude(self):
        tray.shutil.which = lambda _: None
        ok, msg = tray.open_wt_claude("/x", "prompt")
        self.assertFalse(ok)
        self.assertIn("claude not found", msg)

    def test_open_wt_claude_success(self):
        tray.shutil.which = lambda _: "/usr/bin/claude"
        seen = {}

        def fake(path, command=None):
            seen["command"] = command
            return True, None

        tray.open_terminal = fake
        ok, _ = tray.open_wt_claude("/x", "do the thing")
        self.assertTrue(ok)
        self.assertIn("do the thing", seen["command"])


class RemoveWorktreeTest(unittest.TestCase):
    def setUp(self):
        self._run = tray.subprocess.run

    def tearDown(self):
        tray.subprocess.run = self._run

    def test_remove_failure_returns_output(self):
        class R:
            returncode = 1
            stdout = ""
            stderr = "fatal: not a worktree"
        tray.subprocess.run = lambda *a, **k: R()
        ok, msg = tray.remove_worktree("/repo", "/repo/wt")
        self.assertFalse(ok)
        self.assertIn("fatal", msg)

    def test_remove_success_no_branch(self):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        tray.subprocess.run = lambda *a, **k: R()
        ok, _ = tray.remove_worktree("/repo", "/repo/wt")
        self.assertTrue(ok)

    def test_remove_success_with_branch_delete(self):
        calls = {"n": 0}

        def fake_run(argv, **kw):
            calls["n"] += 1

            class R:
                returncode = 0
                stdout = "Deleted branch feat" if "branch" in argv else ""
                stderr = ""
            return R()

        tray.subprocess.run = fake_run
        ok, msg = tray.remove_worktree("/repo", "/repo/wt", branch="feat")
        self.assertTrue(ok)
        self.assertIn("Deleted branch feat", msg)
        self.assertEqual(calls["n"], 2)

    def test_remove_exception(self):
        def raiser(*a, **k):
            raise OSError("boom")
        tray.subprocess.run = raiser
        ok, msg = tray.remove_worktree("/repo", "/repo/wt")
        self.assertFalse(ok)
        self.assertIn("boom", msg)


class CurrentUpstreamAndPushRepoErrorTest(unittest.TestCase):
    def setUp(self):
        self._run = tray.subprocess.run

    def tearDown(self):
        tray.subprocess.run = self._run

    def test_current_upstream_exception_returns_empty(self):
        def raiser(*a, **k):
            raise OSError("boom")
        tray.subprocess.run = raiser
        self.assertEqual(tray._current_upstream("/x", os.environ), "")

    def test_push_repo_timeout(self):
        def fake_run(argv, **kw):
            if "rev-parse" in argv:
                class R:
                    returncode = 0
                    stdout = "origin/main\n"
                return R()
            raise tray.subprocess.TimeoutExpired(cmd=argv, timeout=1)
        tray.subprocess.run = fake_run
        ok, msg = tray.push_repo("/x")
        self.assertFalse(ok)
        self.assertIn("timed out", msg)

    def test_push_repo_oserror(self):
        def fake_run(argv, **kw):
            if "rev-parse" in argv:
                class R:
                    returncode = 0
                    stdout = "origin/main\n"
                return R()
            raise OSError("boom")
        tray.subprocess.run = fake_run
        ok, msg = tray.push_repo("/x")
        self.assertFalse(ok)
        self.assertIn("boom", msg)


class StreamArgvTest(unittest.TestCase):
    def test_commit_stream_argv_flags(self):
        argv = tray.commit_stream_argv("/bin/claude", 5.0)
        self.assertIn("stream-json", argv)
        self.assertIn("--verbose", argv)
        self.assertIn("--max-budget-usd", argv)

    def test_commit_stream_argv_zero_budget_omits_flag(self):
        argv = tray.commit_stream_argv("/bin/claude", 0)
        self.assertNotIn("--max-budget-usd", argv)

    def test_push_claude_stream_argv_flags(self):
        argv = tray.push_claude_stream_argv("/bin/claude", 5.0,
                                            model="opus", effort="high")
        self.assertIn("stream-json", argv)
        self.assertIn("--model", argv)
        self.assertIn("opus", argv)
        self.assertIn("--effort", argv)
        self.assertIn("high", argv)


class CommitRepoMoreTest(unittest.TestCase):
    def setUp(self):
        self._which = tray.shutil.which
        self._run = tray.subprocess.run

    def tearDown(self):
        tray.shutil.which = self._which
        tray.subprocess.run = self._run

    def test_timeout(self):
        tray.shutil.which = lambda _: "/usr/bin/claude"

        def raiser(*a, **k):
            raise tray.subprocess.TimeoutExpired(cmd="claude", timeout=5)
        tray.subprocess.run = raiser
        ok, msg = tray.commit_repo("/x", timeout=5)
        self.assertFalse(ok)
        self.assertIn("timed out after 5s", msg)

    def test_oserror(self):
        tray.shutil.which = lambda _: "/usr/bin/claude"

        def raiser(*a, **k):
            raise OSError("boom")
        tray.subprocess.run = raiser
        ok, msg = tray.commit_repo("/x")
        self.assertFalse(ok)
        self.assertIn("boom", msg)

    def test_success_prefers_json_result(self):
        tray.shutil.which = lambda _: "/usr/bin/claude"

        class R:
            returncode = 0
            stdout = '{"result": "committed 2 changes"}'
            stderr = ""
        tray.subprocess.run = lambda *a, **k: R()
        ok, msg = tray.commit_repo("/x")
        self.assertTrue(ok)
        self.assertEqual(msg, "committed 2 changes")

    def test_nonzero_exit_falls_back_to_raw_output(self):
        tray.shutil.which = lambda _: "/usr/bin/claude"

        class R:
            returncode = 1
            stdout = "not json"
            stderr = ""
        tray.subprocess.run = lambda *a, **k: R()
        ok, msg = tray.commit_repo("/x")
        self.assertFalse(ok)
        self.assertEqual(msg, "not json")


class PushClaudeRepoMoreTest(unittest.TestCase):
    def setUp(self):
        self._which = tray.shutil.which
        self._run = tray.subprocess.run

    def tearDown(self):
        tray.shutil.which = self._which
        tray.subprocess.run = self._run

    def test_timeout(self):
        tray.shutil.which = lambda _: "/usr/bin/claude"

        def raiser(*a, **k):
            raise tray.subprocess.TimeoutExpired(cmd="claude", timeout=5)
        tray.subprocess.run = raiser
        ok, msg = tray.push_claude_repo("/x", timeout=5)
        self.assertFalse(ok)
        self.assertIn("timed out after 5s", msg)

    def test_success_prefers_json_result(self):
        tray.shutil.which = lambda _: "/usr/bin/claude"

        class R:
            returncode = 0
            stdout = '{"result": "pushed"}'
            stderr = ""
        tray.subprocess.run = lambda *a, **k: R()
        ok, msg = tray.push_claude_repo("/x")
        self.assertTrue(ok)
        self.assertEqual(msg, "pushed")


class FmtStreamEventTest(unittest.TestCase):
    def test_non_json_passthrough(self):
        self.assertEqual(tray._fmt_stream_event("plain text"), "plain text")

    def test_assistant_text(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello"}]}})
        self.assertEqual(tray._fmt_stream_event(line), "hello\n")

    def test_assistant_tool_use_command(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "ls -la"}}]}})
        self.assertIn("► Bash: ls -la", tray._fmt_stream_event(line))

    def test_assistant_tool_use_file_path(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit",
             "input": {"file_path": "/x/y.py"}}]}})
        self.assertIn("► Edit: /x/y.py", tray._fmt_stream_event(line))

    def test_assistant_tool_use_other_keys(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Weird", "input": {"foo": "bar"}}]}})
        self.assertIn("► Weird: foo=…", tray._fmt_stream_event(line))

    def test_assistant_tool_use_non_dict_input(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Weird", "input": "raw"}]}})
        self.assertIn("► Weird: raw", tray._fmt_stream_event(line))

    def test_assistant_empty_content_returns_empty(self):
        line = json.dumps({"type": "assistant", "message": {"content": []}})
        self.assertEqual(tray._fmt_stream_event(line), "")

    def test_result_error(self):
        line = json.dumps({"type": "result", "is_error": True, "result": "boom"})
        self.assertTrue(tray._fmt_stream_event(line).startswith("✗"))

    def test_result_success(self):
        line = json.dumps({"type": "result", "is_error": False, "result": "done"})
        self.assertTrue(tray._fmt_stream_event(line).startswith("✓"))

    def test_unknown_type_returns_empty(self):
        self.assertEqual(tray._fmt_stream_event(json.dumps({"type": "system"})), "")


class MemAvailableErrorTest(unittest.TestCase):
    def test_missing_proc_meminfo_returns_zero(self):
        import builtins
        orig_open = builtins.open

        def fake_open(path, *a, **k):
            if path == "/proc/meminfo":
                raise OSError("no such file")
            return orig_open(path, *a, **k)

        builtins.open = fake_open
        try:
            self.assertEqual(tray._mem_available_mb(), 0)
        finally:
            builtins.open = orig_open


class FetchModelTest(unittest.TestCase):
    def setUp(self):
        self._core = tray._core_script
        self._run = tray.subprocess.run

    def tearDown(self):
        tray._core_script = self._core
        tray.subprocess.run = self._run

    def test_core_script_path(self):
        self.assertTrue(tray._core_script().endswith("repodash.py"))

    def test_missing_core_script(self):
        tray._core_script = lambda: "/no/such/repodash.py"
        d = tray.fetch_model()
        self.assertIn("error", d)
        self.assertEqual(d["repos"], [])

    def test_subprocess_error(self):
        tray._core_script = lambda: __file__

        def raiser(*a, **k):
            raise OSError("boom")
        tray.subprocess.run = raiser
        d = tray.fetch_model()
        self.assertIn("boom", d["error"])

    def test_nonzero_exit(self):
        tray._core_script = lambda: __file__

        class R:
            returncode = 1
            stdout = ""
            stderr = "traceback here"
        tray.subprocess.run = lambda *a, **k: R()
        d = tray.fetch_model()
        self.assertIn("traceback here", d["error"])

    def test_bad_json(self):
        tray._core_script = lambda: __file__

        class R:
            returncode = 0
            stdout = "not json"
            stderr = ""
        tray.subprocess.run = lambda *a, **k: R()
        d = tray.fetch_model()
        self.assertIn("bad JSON", d["error"])

    def test_success(self):
        tray._core_script = lambda: __file__

        class R:
            returncode = 0
            stdout = '{"repos": [{"name": "x"}]}'
            stderr = ""
        tray.subprocess.run = lambda *a, **k: R()
        d = tray.fetch_model()
        self.assertEqual(d["repos"], [{"name": "x"}])


class RunCheckTest(unittest.TestCase):
    def setUp(self):
        self._xdg = os.environ.get("XDG_CONFIG_HOME")
        self._tmp = tempfile.mkdtemp(prefix="repodash-runcheck-")
        os.environ["XDG_CONFIG_HOME"] = self._tmp
        self._scan_dirty = tray.scan_dirty
        self._detect_terminal = tray.detect_terminal
        self._resolve_base_dir = tray.resolve_base_dir

    def tearDown(self):
        tray.scan_dirty = self._scan_dirty
        tray.detect_terminal = self._detect_terminal
        tray.resolve_base_dir = self._resolve_base_dir
        if self._xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._xdg
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_prints_all_sections(self):
        tray.resolve_base_dir = lambda cfg: "/fake/base"
        tray.detect_terminal = lambda: "ptyxis"
        repos = [
            {"path": "/repos/clean", "name": "clean", "branch": "main",
             "ahead": 0, "behind": 0, "dirty": False, "count": 0,
             "has_remote": True, "unpushed": 0,
             "stale_worktrees": {"stuck": [], "idle": [], "merged": []}},
            {"path": "/repos/dirty", "name": "dirty", "branch": "main",
             "ahead": 1, "behind": 2, "dirty": True, "count": 3,
             "has_remote": True, "unpushed": 4,
             "stale_worktrees": {
                 "stuck": [{"branch": "feat", "last_commit_age_hours": 20}],
                 "idle": [{"branch": "old", "last_commit_age_hours": 30,
                          "behind": 1}],
                 "merged": [{"branch": "done", "last_commit_age_hours": 50}],
             }},
        ]
        tray.scan_dirty = lambda base, depth, cfg: repos
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = tray.run_check()
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("2 found, 1 dirty, 1 unpushed", out)
        self.assertIn("stuck wt", out)
        self.assertIn("idle wt", out)
        self.assertIn("merged wt", out)
        self.assertIn("unpushed  :", out)

    def test_excluded_and_remoteless_filters_and_no_terminal(self):
        tray.resolve_base_dir = lambda cfg: "/fake/base"
        tray.detect_terminal = lambda: None
        repos = [
            {"path": "/repos/a", "name": "a", "branch": "main",
             "ahead": 0, "behind": 0, "dirty": False, "count": 0,
             "has_remote": False, "unpushed": 0},
            {"path": "/repos/b", "name": "b", "branch": "main",
             "ahead": 0, "behind": 0, "dirty": True, "count": 1,
             "has_remote": True, "unpushed": 0},
        ]
        tray.scan_dirty = lambda base, depth, cfg: repos
        cfg = tray.load_config()
        cfg["excluded_repos"] = ["/repos/a"]
        cfg["show_remoteless"] = False
        tray.save_config(cfg)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tray.run_check()
        out = buf.getvalue()
        self.assertIn("1 found, 1 dirty", out)
        self.assertIn("excluded  :", out)
        self.assertIn("(none found", out)


if __name__ == "__main__":
    unittest.main()

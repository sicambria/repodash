#!/usr/bin/env python3
"""repodash test suite (stdlib unittest, zero dependencies).

Builds a deterministic fixture tree once, then drives both implementations
through their real CLIs. The headline test is ``test_json_parity``: the Python
and bash ``--json`` outputs must be semantically identical — that is the
contract that keeps the two implementations from drifting.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import fixtures

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, "repodash.py")
BASH = os.path.join(ROOT, "repodash")
HAVE_BASH = shutil.which("bash") is not None
HAVE_GIT = shutil.which("git") is not None
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Direct import of the canonical implementation for unit-level testing.
sys.path.insert(0, ROOT)
import repodash as rd

_TREE = None


def setUpModule():
    global _TREE
    if not HAVE_GIT:
        raise unittest.SkipTest("git not available")
    _TREE = tempfile.mkdtemp(prefix="repodash-fix-")
    fixtures.build(_TREE)


def tearDownModule():
    if _TREE and os.path.isdir(_TREE):
        shutil.rmtree(_TREE, ignore_errors=True)


def run_py(*args, env=None):
    return subprocess.run([sys.executable, PY, *args], capture_output=True,
                          text=True, env=_env(env)).stdout


def run_bash(*args, env=None):
    return subprocess.run(["bash", BASH, *args], capture_output=True,
                          text=True, env=_env(env)).stdout


def _env(extra):
    e = dict(os.environ)
    e.pop("NO_COLOR", None)
    e.pop("COLUMNS", None)
    e.pop("SONAR_URL", None)
    e.pop("SONAR_TOKEN", None)
    if extra:
        e.update(extra)
    return e


def normalize(doc_text):
    """Strip volatile fields so two JSON docs can be compared for parity."""
    d = json.loads(doc_text)
    d["generated_at"] = "SENTINEL"
    d["base_dir"] = "BASE"
    for r in d["repos"]:
        r["path"] = os.path.basename(r["path"].rstrip("/"))
    d["repos"].sort(key=lambda r: r["name"])
    return d


def repo(doc, name):
    for r in doc["repos"]:
        if r["name"] == name:
            return r
    raise AssertionError(f"repo {name} not found")


# ── model / section behaviour (Python implementation) ────────────────────────
class TestModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = normalize(run_py(_TREE, "--json"))

    def test_repos_discovered(self):
        names = {r["name"] for r in self.doc["repos"]}
        self.assertEqual(names, fixtures.EXPECTED_REPO_NAMES)

    def test_bare_remote_not_discovered(self):
        self.assertNotIn("_remote.git", {r["name"] for r in self.doc["repos"]})

    def test_diverged_ahead_behind(self):
        g = repo(self.doc, "diverged")["git"]
        self.assertEqual((g["ahead"], g["behind"]), (1, 1))
        self.assertTrue(g["dirty"])

    def test_todos_total_and_shown(self):
        t = repo(self.doc, "repoA")["todos"]
        self.assertEqual(t["total"], 15)
        self.assertEqual(t["shown"], 10)
        self.assertEqual(len(t["items"]), 15)  # JSON carries the full list

    def test_audit_archive_grouping(self):
        a = repo(self.doc, "repoB")["audit"]
        self.assertEqual(a["archive"]["count"], 3)
        self.assertEqual(a["archive"]["most_recent"], "2024-03-01-audit.md")
        self.assertEqual(a["archive"]["open_items_total"], 4)

    def test_checklist_indented_and_star_stripped(self):
        items = repo(self.doc, "repoD")["roadmap"]["files"][0]["items"]
        texts = [i["text"] for i in items]
        self.assertIn("sub feature", texts)     # indented "  - [ ]"
        self.assertIn("star feature", texts)    # "* [ ]" bullet
        self.assertNotIn("done thing", texts)   # "[x]" excluded
        for t in texts:                         # markers fully stripped
            self.assertNotRegex(t, r"\[ \]")

    def test_backslash_preserved_in_json(self):
        text = repo(self.doc, "my repo")["todos"]["items"][0]["text"]
        self.assertIn("\\t", text)
        self.assertIn("C:\\new", text)

    def test_sonar_configured_no_url(self):
        s = repo(self.doc, "sonarrepo")["sonar"]
        self.assertTrue(s["configured"])
        self.assertFalse(s["ok"])
        self.assertIn("SONAR_URL", s["error"])

    def test_schema_keys_present(self):
        self.assertEqual(self.doc["schema_version"], 1)
        for r in self.doc["repos"]:
            for key in ("git", "todos", "audit", "roadmap", "sonar"):
                self.assertIn(key, r)


# ── sonar onboarding audit (model + render) ──────────────────────────────────
class TestOnboarding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = normalize(run_py(_TREE, "--json"))
        cls.render = run_py(_TREE, "--sonar", "--no-color", "--width", "100")

    def _onb(self, name):
        return repo(self.doc, name)["sonar"]["onboarding"]

    def test_not_onboarded_flag(self):
        s = repo(self.doc, "jsnoonboard")["sonar"]
        self.assertFalse(s["configured"])
        self.assertTrue(s["onboarding"]["has_package_json"])
        self.assertIsNone(s["onboarding"]["optout_reason"])
        self.assertIn("⚠ code project not onboarded to Sonar", self.render)
        self.assertIn("no sonar-project.properties — add one, or a .sonar-optout",
                      self.render)

    def test_no_gate_flag(self):
        s = repo(self.doc, "jsgateless")["sonar"]
        self.assertTrue(s["configured"])
        self.assertTrue(s["onboarding"]["has_package_json"])
        self.assertFalse(s["onboarding"]["has_sonar_gate"])
        self.assertIn("⚠ no pre-push sonar:gate ratchet", self.render)
        self.assertIn("(onboarded but drift is ungated)", self.render)

    def test_gate_present_no_flag(self):
        # onboarded + a real sonar:gate script → no warning, no false positive
        self.assertTrue(self._onb("jsgated")["has_sonar_gate"])

    def test_optout_not_onboarded(self):
        onb = self._onb("optoutnoonboard")
        self.assertEqual(onb["optout_reason"],
                         "prototype spike — not worth onboarding yet")
        self.assertIn("not onboarded — opt-out: prototype spike", self.render)

    def test_optout_gate(self):
        onb = self._onb("optoutgate")
        self.assertEqual(onb["optout_reason"],
                         "D-011: single-contributor repo, local scans only")
        self.assertIn("no pre-push sonar:gate ratchet — opt-out: D-011", self.render)

    def test_optout_suppresses_warning(self):
        # a .sonar-optout marker must replace the yellow ⚠ with a dim note
        self.assertNotIn("⚠ code project not onboarded to Sonar\n"
                         "  no sonar-project.properties", self.render)
        # the opt-out repos never emit the bare warning phrasing
        for line in self.render.splitlines():
            if "opt-out" in line:
                self.assertNotIn("⚠", line)

    def test_non_js_repo_no_flag(self):
        # repoC has no package.json → no onboarding signal, no sonar line at all
        onb = self._onb("repoC")
        self.assertFalse(onb["has_package_json"])
        self.assertFalse(repo(self.doc, "repoC")["sonar"]["configured"])


# ── rendering ────────────────────────────────────────────────────────────────
class TestRender(unittest.TestCase):
    def test_backslash_literal_in_render(self):
        # the exact-string match proves \t/\new render as literal backslashes,
        # not as a real tab / newline (which would break this comparison).
        out = run_py(_TREE, "--todos", "--no-color", "--width", "100")
        self.assertIn(r"// TODO: handle \t tabs and C:\new path", out)

    def test_and_more_indicator(self):
        out = run_py(_TREE, "--todos", "--no-color", "--width", "100")
        self.assertIn("… and 5 more", out)

    def test_no_ansi_when_piped(self):
        out = run_py(_TREE, "--width", "100")  # piped → no tty → no color
        self.assertNotIn("\x1b[", out)

    def test_pluralization(self):
        single = run_py(os.path.join(_TREE, "repoA"), "--no-color", "--width", "80")
        self.assertIn(" 1 repo\n", single)
        self.assertIn("1 repo have items", single)

    def test_clean_repo(self):
        out = run_py(os.path.join(_TREE, "repoC"), "--no-color", "--width", "80")
        self.assertIn("✓ clean", out)


# ── parity gate (the contract) ───────────────────────────────────────────────
@unittest.skipUnless(HAVE_BASH, "bash not available")
class TestParity(unittest.TestCase):
    def test_json_parity(self):
        py = normalize(run_py(_TREE, "--json"))
        sh = normalize(run_bash(_TREE, "--json"))
        self.assertEqual(py, sh, "Python and bash --json diverged")

    def test_render_parity(self):
        py = run_py(_TREE, "--no-color", "--width", "100")
        sh = run_bash(_TREE, "--no-color", "--width", "100")
        self.assertEqual(py, sh, "Python and bash rendered output diverged")

    def test_dirty_filter_parity(self):
        py = run_py(_TREE, "--dirty", "--no-color", "--width", "100")
        sh = run_bash(_TREE, "--dirty", "--no-color", "--width", "100")
        self.assertEqual(py, sh)

    def test_ere_fallback_parity(self):
        """Forcing bash's POSIX-ERE path (no grep -P) must still match Python."""
        py = normalize(run_py(_TREE, "--json"))
        sh = normalize(run_bash(_TREE, "--json", env={"REPODASH_FORCE_ERE": "1"}))
        self.assertEqual(py, sh)


# ── sonar over a real (mock) HTTP endpoint, exercised by both impls ──────────
class _SonarHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if "badkey" in self.path:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"errors":[{"msg":"Insufficient privileges"}]}')
        elif "missingkey" in self.path:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"component": {"measures": [
                {"metric": "bugs", "value": "3"},
                {"metric": "vulnerabilities", "value": "0"},
                {"metric": "security_hotspots", "value": "2"},
                {"metric": "code_smells", "value": "12"},
                {"metric": "coverage", "value": "84.5"},
                {"metric": "duplicated_lines_density", "value": "1.2"},
            ]}}).encode())

    def log_message(self, *a):
        pass


class TestSonar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _SonarHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.dir = tempfile.mkdtemp(prefix="repodash-sonar-")
        cls._mk("okrepo", "okkey")
        cls._mk("badrepo", "badkey")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def _mk(cls, name, key):
        path = os.path.join(cls.dir, name)
        fixtures._init(path)
        fixtures._write(os.path.join(path, "sonar-project.properties"),
                        f"sonar.projectKey={key}\n")
        fixtures._commit(path, "init")

    def _sonar(self, name, token="tok", insecure=False):
        env = {"SONAR_URL": self.url, "SONAR_TOKEN": token}
        args = [os.path.join(self.dir, name), "--json"]
        doc = json.loads(run_py(*args, env=env))
        return doc["repos"][0]["sonar"]

    def test_metrics_ok(self):
        s = self._sonar("okrepo")
        self.assertTrue(s["ok"])
        self.assertEqual(s["metrics"]["bugs"], 3)
        self.assertEqual(s["metrics"]["coverage"], 84.5)

    def test_http_error_surfaces_message(self):
        s = self._sonar("badrepo")
        self.assertFalse(s["ok"])
        self.assertEqual(s["error"], "Insufficient privileges")

    def test_unreachable(self):
        env = {"SONAR_URL": "http://127.0.0.1:1"}
        doc = json.loads(run_py(os.path.join(self.dir, "okrepo"), "--json", env=env))
        s = doc["repos"][0]["sonar"]
        self.assertFalse(s["ok"])
        self.assertIn("unreachable", s["error"])

    def test_token_never_leaks(self):
        env = {"SONAR_URL": self.url, "SONAR_TOKEN": "supersecret"}
        out = run_py(os.path.join(self.dir, "okrepo"), "--json", env=env)
        self.assertNotIn("supersecret", out)

    @unittest.skipUnless(HAVE_BASH, "bash not available")
    def test_sonar_parity(self):
        env = {"SONAR_URL": self.url, "SONAR_TOKEN": "tok"}
        py = json.loads(run_py(self.dir, "--json", env=env))
        sh = json.loads(run_bash(self.dir, "--json", env=env))
        for d in (py, sh):
            d["generated_at"] = "S"
            d["base_dir"] = "B"
            for r in d["repos"]:
                r["path"] = os.path.basename(r["path"])
            d["repos"].sort(key=lambda r: r["name"])
        self.assertEqual(py, sh)


# ── ERE fallback pattern equivalence (covers non-PCRE platforms) ─────────────
class TestEREFallback(unittest.TestCase):
    """The bash ERE fallback must match the same lines as the PCRE pattern."""

    PCRE = re.compile(r"(?://|#|<!--|--|\*)\s*(TODO|FIXME|HACK)\b")
    # mirror of the bash ERE pattern (Python re uses POSIX classes happily)
    ERE = re.compile(r"(//|#|<!--|--|[*])[[:space:]]*(TODO|FIXME|HACK)([^A-Za-z0-9_]|$)"
                     .replace("[[:space:]]", r"\s"))

    SAMPLES = [
        ("// TODO: x", True),
        ("# FIXME later", True),
        ("  * HACK around", True),
        ("-- TODO sql", True),
        ("<!-- TODO html -->", True),
        ("TODO_DIR = '/x'", False),       # not in comment context
        ("todoList.append(1)", False),    # lowercase / no marker
        ("int FIXMEABLE = 1", False),     # \b boundary
    ]

    def test_equivalence(self):
        for line, expected in self.SAMPLES:
            self.assertEqual(bool(self.PCRE.search(line)), expected, line)
            self.assertEqual(bool(self.ERE.search(line)), expected, line)


# ── direct-import unit tests (config / CLI helpers) ──────────────────────────
class TestConfigCLI(unittest.TestCase):
    def test_coerce_int_returns_int(self):
        self.assertEqual(rd._coerce("42"), 42)
        self.assertIsInstance(rd._coerce("42"), int)

    def test_coerce_float_returns_float(self):
        self.assertEqual(rd._coerce("3.14"), 3.14)
        self.assertIsInstance(rd._coerce("3.14"), float)

    def test_coerce_non_numeric_returns_none(self):
        self.assertIsNone(rd._coerce("abc"))
        self.assertIsNone(rd._coerce(""))

    def test_coerce_negative_int(self):
        self.assertEqual(rd._coerce("-5"), -5)

    def test_parse_args_defaults(self):
        args = rd.parse_args(["dir"])
        self.assertEqual(args.dir, "dir")
        self.assertFalse(args.as_json)
        self.assertFalse(args.no_color)
        self.assertEqual(args.depth, 3)

    def test_parse_args_section_flags_set(self):
        args = rd.parse_args(["dir", "--git", "--todos"])
        self.assertTrue(args.git)
        self.assertTrue(args.todos)
        self.assertFalse(args.audit)

    def test_parse_args_json_mode(self):
        args = rd.parse_args(["--json"])
        self.assertTrue(args.as_json)

    def test_build_config_default_show_all(self):
        args = rd.parse_args(["dir"])
        cfg = rd.build_config(args)
        self.assertTrue(cfg.show_git)
        self.assertTrue(cfg.show_todos)
        self.assertTrue(cfg.show_audit)
        self.assertTrue(cfg.show_roadmap)
        self.assertTrue(cfg.show_sonar)

    def test_build_config_section_only(self):
        args = rd.parse_args(["dir", "--git"])
        cfg = rd.build_config(args)
        self.assertTrue(cfg.show_git)
        self.assertFalse(cfg.show_todos)

    def test_build_config_env_vars(self):
        saved_url = os.environ.get("SONAR_URL")
        saved_token = os.environ.get("SONAR_TOKEN")
        try:
            os.environ["SONAR_URL"] = "http://s:9000"
            os.environ["SONAR_TOKEN"] = "secret"
            args = rd.parse_args(["dir"])
            cfg = rd.build_config(args)
            self.assertEqual(cfg.sonar_url, "http://s:9000")
            self.assertEqual(cfg.sonar_token, "secret")
        finally:
            if saved_url is None:
                os.environ.pop("SONAR_URL", None)
            else:
                os.environ["SONAR_URL"] = saved_url
            if saved_token is None:
                os.environ.pop("SONAR_TOKEN", None)
            else:
                os.environ["SONAR_TOKEN"] = saved_token

    def test_build_config_json_ignores_section_flags(self):
        args = rd.parse_args(["dir", "--json", "--git"])
        cfg = rd.build_config(args)
        self.assertTrue(cfg.as_json)
        self.assertTrue(cfg.show_git)

    def test_build_config_dirty_flag(self):
        args = rd.parse_args(["dir", "--dirty"])
        cfg = rd.build_config(args)
        self.assertTrue(cfg.only_dirty)

    def test_resolve_width_override(self):
        cfg = rd.Config()
        cfg.width = 120
        self.assertEqual(rd.resolve_width(cfg), 120)

    def test_resolve_width_columns_env(self):
        saved = os.environ.get("COLUMNS")
        try:
            os.environ["COLUMNS"] = "99"
            cfg = rd.Config()
            cfg.width = None
            self.assertEqual(rd.resolve_width(cfg), 99)
        finally:
            if saved is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = saved

    def test_resolve_width_fallback(self):
        saved = os.environ.get("COLUMNS")
        try:
            os.environ.pop("COLUMNS", None)
            cfg = rd.Config()
            cfg.width = None
            w = rd.resolve_width(cfg)
            self.assertGreater(w, 0)
        finally:
            if saved is not None:
                os.environ["COLUMNS"] = saved


# ── direct-import unit tests (helpers / error-paths) ──────────────────────────
class TestHelpersDirect(unittest.TestCase):
    def setUp(self):
        self._orig_subprocess_run = rd.subprocess.run

    def tearDown(self):
        rd.subprocess.run = self._orig_subprocess_run

    def test_git_returns_empty_on_subprocess_error(self):
        def raiser(*a, **k):
            raise OSError("boom")
        rd.subprocess.run = raiser
        self.assertEqual(rd._git("/tmp", "status"), "")

    def test_git_returns_empty_on_timeout(self):
        def raiser(*a, **k):
            raise subprocess.TimeoutExpired(cmd=["git"], timeout=1)
        rd.subprocess.run = raiser
        self.assertEqual(rd._git("/tmp", "status"), "")

    def test_git_returns_empty_on_missing_dir(self):
        self.assertEqual(rd._git("/no/such/dir/xyz", "status"), "")

    def test_read_lines_oserror_returns_empty(self):
        self.assertEqual(rd._read_lines("/no/such/file/xyz.txt"), [])

    def test_read_lines_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("")
        try:
            self.assertEqual(rd._read_lines(f.name), [])
        finally:
            os.remove(f.name)

    def test_read_props_key_and_host(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".properties") as f:
            f.write("sonar.projectKey=mykey\nsonar.host.url=https://sonar.example.com\n")
        try:
            key, host = rd._read_props(f.name)
            self.assertEqual(key, "mykey")
            self.assertEqual(host, "https://sonar.example.com")
        finally:
            os.remove(f.name)

    def test_read_props_key_only_no_host(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".properties") as f:
            f.write("sonar.projectKey=solekey\n# comment\n")
        try:
            key, host = rd._read_props(f.name)
            self.assertEqual(key, "solekey")
            self.assertIsNone(host)
        finally:
            os.remove(f.name)

    def test_read_props_missing_file(self):
        key, host = rd._read_props("/no/such/file.properties")
        self.assertIsNone(key)
        self.assertIsNone(host)

    def test_read_props_no_section(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".properties") as f:
            f.write("# just a comment\nsome.other.key=value\n")
        try:
            key, host = rd._read_props(f.name)
            self.assertIsNone(key)
            self.assertIsNone(host)
        finally:
            os.remove(f.name)

    def test_repo_has_content_dirty_only(self):
        cfg = rd.Config()
        cfg.show_git = True
        cfg.show_todos = cfg.show_audit = cfg.show_roadmap = cfg.show_sonar = False
        m = {"git": {"dirty": True}, "todos": {"total": 0},
             "audit": {"files": [], "archive": None},
             "roadmap": {"files": []},
             "sonar": {"configured": False, "onboarding": {"has_package_json": False}}}
        self.assertTrue(rd.repo_has_content(m, cfg))

    def test_repo_has_content_todos_only(self):
        cfg = rd.Config()
        cfg.show_todos = True
        cfg.show_git = cfg.show_audit = cfg.show_roadmap = cfg.show_sonar = False
        m = {"git": {"dirty": False}, "todos": {"total": 5},
             "audit": {"files": [], "archive": None},
             "roadmap": {"files": []},
             "sonar": {"configured": False, "onboarding": {"has_package_json": False}}}
        self.assertTrue(rd.repo_has_content(m, cfg))

    def test_repo_has_content_audit_only_with_files(self):
        cfg = rd.Config()
        cfg.show_audit = True
        cfg.show_git = cfg.show_todos = cfg.show_roadmap = cfg.show_sonar = False
        m = {"git": {"dirty": False}, "todos": {"total": 0},
             "audit": {"files": [{"items": []}], "archive": None},
             "roadmap": {"files": []},
             "sonar": {"configured": False, "onboarding": {"has_package_json": False}}}
        self.assertTrue(rd.repo_has_content(m, cfg))

    def test_repo_has_content_roadmap_only(self):
        cfg = rd.Config()
        cfg.show_roadmap = True
        cfg.show_git = cfg.show_todos = cfg.show_audit = cfg.show_sonar = False
        m = {"git": {"dirty": False}, "todos": {"total": 0},
             "audit": {"files": [], "archive": None},
             "roadmap": {"files": [{"items": [{"text": "do X"}]}]},
             "sonar": {"configured": False, "onboarding": {"has_package_json": False}}}
        self.assertTrue(rd.repo_has_content(m, cfg))

    def test_repo_has_content_sonar_configured(self):
        cfg = rd.Config()
        cfg.show_sonar = True
        cfg.show_git = cfg.show_todos = cfg.show_audit = cfg.show_roadmap = False
        m = {"git": {"dirty": False}, "todos": {"total": 0},
             "audit": {"files": [], "archive": None},
             "roadmap": {"files": []},
             "sonar": {"configured": True, "onboarding": {"has_package_json": True}}}
        self.assertTrue(rd.repo_has_content(m, cfg))

    def test_repo_has_content_nothing(self):
        cfg = rd.Config()
        cfg.show_git = cfg.show_todos = cfg.show_audit = cfg.show_roadmap = cfg.show_sonar = False
        m = {"git": {"dirty": True}, "todos": {"total": 10},
             "audit": {"files": [{"items": []}], "archive": {"count": 1}},
             "roadmap": {"files": [{"items": [{"text": "x"}]}]},
             "sonar": {"configured": True, "onboarding": {"has_package_json": True}}}
        self.assertFalse(rd.repo_has_content(m, cfg))

    def test_repo_has_content_clean_repo(self):
        cfg = rd.Config()
        cfg.show_git = True
        cfg.show_todos = cfg.show_audit = cfg.show_roadmap = cfg.show_sonar = True
        m = {"git": {"dirty": False}, "todos": {"total": 0},
             "audit": {"files": [], "archive": None},
             "roadmap": {"files": []},
             "sonar": {"configured": False, "onboarding": {"has_package_json": False}}}
        self.assertFalse(rd.repo_has_content(m, cfg))

    def test_process_repo_error_model_structure(self):
        cfg = rd.build_config(rd.parse_args(["dir"]))
        m = rd.process_repo("/no/such/dir", cfg)
        self.assertIn("path", m)
        self.assertIn("name", m)
        self.assertIn("git", m)
        self.assertIn("todos", m)
        self.assertIn("audit", m)
        self.assertIn("roadmap", m)
        self.assertIn("sonar", m)

    def test_process_repo_nonexistent_dir_does_not_crash(self):
        cfg = rd.build_config(rd.parse_args(["dir"]))
        m = rd.process_repo("/no/such/path/xyz/nope", cfg)
        self.assertEqual(m["todos"]["total"], 0)
        self.assertFalse(m["git"]["dirty"])

    def test_row_formatting(self):
        p = rd.Palette(False)
        out = rd._row(p, "git", "content")
        self.assertIn("git", out)
        self.assertIn("content", out)

    def test_section_git_no_repo(self):
        g = rd.section_git("/no/such/repo")
        self.assertFalse(g["dirty"])
        self.assertEqual(g["ahead"], 0)
        self.assertEqual(g["behind"], 0)


# ── direct-import unit tests (find_repos) ────────────────────────────────────
class TestFindRepos(unittest.TestCase):
    def test_depth_zero_finds_top_level(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "repo", ".git"))
            repos = rd.find_repos(base, depth=1)
            self.assertIn(os.path.join(base, "repo"), repos)

    def test_depth_limits_skip_deep(self):
        with tempfile.TemporaryDirectory() as base:
            deep = os.path.join(base, "a", "b", "c")
            os.makedirs(os.path.join(deep, ".git"))
            repos = rd.find_repos(base, depth=1)
            self.assertEqual(repos, [])

    def test_nested_git_not_descended(self):
        with tempfile.TemporaryDirectory() as base:
            repo = os.path.join(base, "outer")
            os.makedirs(os.path.join(repo, ".git"))
            os.makedirs(os.path.join(repo, "sub", ".git"))
            repos = rd.find_repos(base, depth=5)
            names = [os.path.basename(r) for r in repos]
            self.assertEqual(names, ["outer"])

    def test_no_git_dirs_returns_empty(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "sub", "deeper"))
            self.assertEqual(rd.find_repos(base, depth=3), [])

    def test_sorts_alphabetically(self):
        with tempfile.TemporaryDirectory() as base:
            for name in ("zebra", "alpha", "gamma"):
                os.makedirs(os.path.join(base, name, ".git"))
            repos = rd.find_repos(base, depth=2)
            names = [os.path.basename(r) for r in repos]
            self.assertEqual(names, ["alpha", "gamma", "zebra"])


# ── direct-import unit tests (palette / render helpers) ──────────────────────
class TestPaletteRender(unittest.TestCase):
    def test_palette_enabled_has_all_colors(self):
        p = rd.Palette(True)
        attrs = ("RED", "YELLOW", "GREEN", "BLUE", "CYAN", "MAGENTA", "BOLD", "DIM", "NC")
        for attr in attrs:
            self.assertTrue(getattr(p, attr), f"Palette.{attr} should be non-empty")

    def test_palette_disabled_all_empty(self):
        p = rd.Palette(False)
        attrs = ("RED", "YELLOW", "GREEN", "BLUE", "CYAN", "MAGENTA", "BOLD", "DIM", "NC")
        for attr in attrs:
            self.assertEqual(getattr(p, attr), "", f"Palette.{attr} should be empty")

    def test_render_git_no_dirty_returns_empty(self):
        p = rd.Palette(False)
        self.assertEqual(rd._render_git({"dirty": False, "ahead": 0, "behind": 0, "dirty_files": []}, p), [])

    def test_render_git_dirty_with_ahead_behind(self):
        p = rd.Palette(False)
        g = {"dirty": True, "ahead": 2, "behind": 1,
             "dirty_files": [{"status": "M", "path": "f.txt"}]}
        out = rd._render_git(g, p)
        self.assertTrue(out)
        self.assertIn("ahead 2", out[0])
        self.assertIn("behind 1", out[0])

    def test_render_todos_with_truncation(self):
        p = rd.Palette(False)
        todos = {"total": 15, "shown": 3, "items": [
            {"path": "f.py", "line": i, "text": f"TODO {i}"} for i in range(15)
        ]}
        cfg = rd.Config()
        cfg.max_todos = 3
        out = rd._render_todos(todos, cfg, p)
        texts = "\n".join(out)
        self.assertIn("… and 12 more", texts)

    def test_render_audit_empty(self):
        p = rd.Palette(False)
        audit = {"files": [], "archive": None}
        out = rd._render_audit(audit, p)
        self.assertEqual(out, [])

    def test_render_roadmap_empty(self):
        p = rd.Palette(False)
        roadmap = {"files": []}
        out = rd._render_roadmap(roadmap, p)
        self.assertEqual(out, [])

    def test_render_sonar_not_configured_no_package_json(self):
        p = rd.Palette(False)
        sonar = {"configured": False, "ok": None, "error": None,
                 "project_key": None, "metrics": None,
                 "onboarding": {"has_package_json": False, "has_sonar_gate": False,
                                "optout_reason": None}}
        out = rd._render_sonar(sonar, p)
        self.assertEqual(out, [])

    def test_render_sonar_not_configured_with_package_json(self):
        p = rd.Palette(False)
        sonar = {"configured": False, "ok": None, "error": None,
                 "project_key": None, "metrics": None,
                 "onboarding": {"has_package_json": True, "has_sonar_gate": False,
                                "optout_reason": None}}
        out = rd._render_sonar(sonar, p)
        self.assertTrue(out)
        self.assertIn("not onboarded", out[0])

    def test_render_sonar_configured_no_gate_no_optout(self):
        p = rd.Palette(False)
        sonar = {"configured": True, "ok": False, "error": "API unreachable",
                 "project_key": "k", "metrics": None,
                 "onboarding": {"has_package_json": True, "has_sonar_gate": False,
                                "optout_reason": None}}
        out = rd._render_sonar(sonar, p)
        texts = "\n".join(out)
        self.assertIn("no pre-push sonar:gate ratchet", texts)
        self.assertIn("API unreachable", texts)

    def test_render_sonar_with_optout(self):
        p = rd.Palette(False)
        sonar = {"configured": False, "ok": None, "error": None,
                 "project_key": None, "metrics": None,
                 "onboarding": {"has_package_json": True, "has_sonar_gate": False,
                                "optout_reason": "D-011: experiment"}}
        out = rd._render_sonar(sonar, p)
        texts = "\n".join(out)
        self.assertIn("opt-out: D-011", texts)

    def test_render_sonar_metrics_ok(self):
        p = rd.Palette(False)
        sonar = {"configured": True, "ok": True, "error": None,
                 "project_key": "k",
                 "metrics": {"bugs": 0, "vulnerabilities": 0,
                             "security_hotspots": 0, "code_smells": 0,
                             "coverage": 85.0, "duplicated_lines_density": 1.5},
                 "onboarding": {"has_package_json": True, "has_sonar_gate": True,
                                "optout_reason": None}}
        out = rd._render_sonar(sonar, p)
        texts = "\n".join(out)
        self.assertIn("bugs:0", texts)
        self.assertIn("coverage:85.0%", texts)


# ── direct tests of main() entry point ───────────────────────────────────────
class TestMain(unittest.TestCase):
    def test_nonexistent_dir_returns_1(self):
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            rc = rd.main(["/no/such/dir/xyz"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", buf.getvalue())

    def test_no_repos_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = rd.main([d, "--no-color"])
            self.assertEqual(rc, 0)
            self.assertIn("No git repositories found", buf.getvalue())

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_json_output_has_schema_version(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = rd.main([_TREE, "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["schema_version"], 1)
        self.assertIn("repos", doc)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_only_dirty_skips_clean_repos(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = rd.main([_TREE, "--dirty", "--no-color", "--width", "100"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertNotIn("repoC", out)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_section_only_git_flag(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = rd.main([_TREE, "--git", "--no-color", "--width", "100"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertNotIn("TODO", out)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_process_repo_exception_is_caught(self):
        original_process_repo = rd.process_repo
        called = [0]

        def raiser(repo, cfg):
            called[0] += 1
            raise RuntimeError("simulated error")

        rd.process_repo = raiser
        try:
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = rd.main([_TREE, "--json"])
            self.assertEqual(rc, 0)
            self.assertGreater(called[0], 0)
            doc = json.loads(buf.getvalue())
            names = {r["name"] for r in doc["repos"]}
            self.assertEqual(names, fixtures.EXPECTED_REPO_NAMES)
            for r in doc["repos"]:
                self.assertIn("error", r)
        finally:
            rd.process_repo = original_process_repo


# ── pre-push hook regression tests ──────────────────────────────────────────
PRE_PUSH_SCRIPT = os.path.join(ROOT, "scripts", "git-hooks", "pre-push")


def _run_hook(stdin_text=""):
    """Run the pre-push hook with REPODASH_SKIP_SLOW_CHECKS=1 and return
    the CompletedProcess (containing stdout, stderr, returncode)."""
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    env.pop("COLUMNS", None)
    return subprocess.run(
        ["bash", PRE_PUSH_SCRIPT],
        input=stdin_text,
        capture_output=True,
        text=True,
        env={**env, "REPODASH_SKIP_SLOW_CHECKS": "1"},
    )


class TestPrePushHook(unittest.TestCase):
    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_empty_stdin_prints_diagnostic_and_exits_zero(self):
        cp = _run_hook("")
        self.assertEqual(cp.returncode, 0)
        self.assertIn("no refs found on stdin", cp.stdout)
        self.assertIn("all checks passed", cp.stdout)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_empty_stdin_does_not_run_secret_scan(self):
        cp = _run_hook("")
        self.assertNotIn("WARNING: baseline SHA", cp.stdout)
        self.assertNotIn("WARNING: local SHA", cp.stdout)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_corrupt_stdin_warns_and_exits_zero(self):
        cp = _run_hook("garbage junk")
        self.assertEqual(cp.returncode, 0)
        self.assertIn("WARNING: malformed stdin line", cp.stderr)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_blank_stdin_line_is_skipped_and_exits_zero(self):
        cp = _run_hook("\n")
        self.assertEqual(cp.returncode, 0)
        self.assertIn("no refs found on stdin", cp.stdout)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_missing_remote_sha_gracefully_skips_scan(self):
        real_sha = "a2f09acd5614721182f4ab834f08416f988f6947"
        cp = _run_hook(
            f"refs/heads/main deadbeefbadcafe refs/heads/main {real_sha}\n"
        )
        self.assertEqual(cp.returncode, 0)
        self.assertIn("all checks passed", cp.stdout)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_pre_push_script_has_err_trap(self):
        with open(PRE_PUSH_SCRIPT) as f:
            content = f.read()
        self.assertIn("trap", content, "ERR trap is required for diagnostics")


if __name__ == "__main__":
    unittest.main(verbosity=2)

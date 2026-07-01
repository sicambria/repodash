#!/usr/bin/env python3
"""repodash test suite (stdlib unittest, zero dependencies).

Builds a deterministic fixture tree once, then drives both implementations
through their real CLIs. The headline test is ``test_json_parity``: the Python
and bash ``--json`` outputs must be semantically identical — that is the
contract that keeps the two implementations from drifting.
"""
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

import fixtures

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, "repodash.py")
BASH = os.path.join(ROOT, "repodash")
HAVE_BASH = shutil.which("bash") is not None
HAVE_GIT = shutil.which("git") is not None
ANSI = re.compile(r"\x1b\[[0-9;]*m")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

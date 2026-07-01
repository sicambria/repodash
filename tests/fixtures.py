#!/usr/bin/env python3
"""Shared, language-neutral fixture builder for repodash tests.

Both the Python ``unittest`` suite and the bash parity harness build the same
deterministic tree of git repos through this module, so the two implementations
are exercised against identical inputs. Run directly to materialise a tree:

    python3 fixtures.py /path/to/root
"""
from __future__ import annotations

import os
import subprocess
import sys

# Deterministic git identity/time so commits (and therefore status) are stable
# regardless of the host's global gitconfig or timezone.
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "repodash-test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "repodash-test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
    "TZ": "UTC",
}


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, env=GIT_ENV,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _init(path):
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    return path


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def _commit(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


def build(root):
    """Create the full fixture tree under *root* and return *root*."""
    os.makedirs(root, exist_ok=True)

    # repoA — 15 TODO markers in a single file (exercises "… and N more")
    a = _init(os.path.join(root, "repoA"))
    _write(os.path.join(a, "app.js"),
           "".join(f"// TODO: task number {i}\n" for i in range(1, 16)))
    _commit(a, "init")

    # repoB — active audit doc (indented/star/backslash items) + dated archive
    b = _init(os.path.join(root, "repoB"))
    _write(os.path.join(b, "SECURITY.md"),
           "# Security review\n- [ ] top item\n   - [ ] deeply indented\n"
           "* [ ] star one\n- [ ] path C:\\new\\thing\n")
    _write(os.path.join(b, "audits", "2024-01-01-audit.md"), "# audit\n")
    _write(os.path.join(b, "audits", "2024-02-01-audit.md"), "# audit\n- [x] done\n")
    _write(os.path.join(b, "audits", "2024-03-01-audit.md"),
           "# audit\n- [ ] open one\n  - [ ] indented sub-task\n"
           "* [ ] star bullet\n- [ ] handle \\t backslash case\n")
    _commit(b, "init")

    # repoC — clean
    c = _init(os.path.join(root, "repoC"))
    _write(os.path.join(c, "readme.txt"), "hi\n")
    _commit(c, "init")

    # "my repo" — space in name, dirty working tree, backslash TODO
    m = _init(os.path.join(root, "my repo"))
    _write(os.path.join(m, "main.go"), "// TODO: handle \\t tabs and C:\\new path\n")
    _commit(m, "init")
    _write(os.path.join(m, "dirty.txt"), "uncommitted\n")  # left untracked

    # repoD — roadmap with indented/star/backslash items
    d = _init(os.path.join(root, "repoD"))
    _write(os.path.join(d, "ROADMAP.md"),
           "# Roadmap\n- [ ] ship v1\n  - [ ] sub feature\n* [ ] star feature\n"
           "- [ ] regex \\d+ support\n- [x] done thing\n")
    _commit(d, "init")

    # special — a TODO whose text contains JSON-significant bytes: a double
    # quote, a real tab, and an ESC control char. Exercises the bash json_str
    # escaper against Python's json.dumps via the parity gate.
    sp = _init(os.path.join(root, "special"))
    _write(os.path.join(sp, "weird.py"),
           '# TODO: quote " and tab\there and esc \x1b end\n')
    _commit(sp, "init")

    # sonarrepo — sonar-project.properties but no URL configured
    s = _init(os.path.join(root, "sonarrepo"))
    _write(os.path.join(s, "sonar-project.properties"), "sonar.projectKey=mykey\n")
    _commit(s, "init")

    # ── Sonar onboarding audit fixtures ──────────────────────────────────────
    # jsnoonboard — JS project (package.json), NOT onboarded → not-onboarded flag
    jn = _init(os.path.join(root, "jsnoonboard"))
    _write(os.path.join(jn, "package.json"),
           '{"name": "jsnoonboard", "scripts": {"build": "tsc"}}\n')
    _commit(jn, "init")

    # jsgateless — onboarded (properties) + package.json with NO sonar:gate → no-gate flag
    jg = _init(os.path.join(root, "jsgateless"))
    _write(os.path.join(jg, "sonar-project.properties"), "sonar.projectKey=jsgateless\n")
    _write(os.path.join(jg, "package.json"),
           '{"name": "jsgateless", "scripts": {"test": "jest"}}\n')
    _commit(jg, "init")

    # jsgated — onboarded + package.json WITH a sonar:gate script → no flag
    jd = _init(os.path.join(root, "jsgated"))
    _write(os.path.join(jd, "sonar-project.properties"), "sonar.projectKey=jsgated\n")
    _write(os.path.join(jd, "package.json"),
           '{"name": "jsgated", "scripts": {"sonar:gate": "node scripts/gate.js"}}\n')
    _commit(jd, "init")

    # optoutnoonboard — not onboarded but a .sonar-optout marker → dim opt-out note
    on = _init(os.path.join(root, "optoutnoonboard"))
    _write(os.path.join(on, "package.json"),
           '{"name": "optoutnoonboard", "scripts": {"build": "tsc"}}\n')
    _write(os.path.join(on, ".sonar-optout"),
           "prototype spike — not worth onboarding yet\n")
    _commit(on, "init")

    # optoutgate — onboarded, no sonar:gate, but a .sonar-optout marker → dim opt-out note
    og = _init(os.path.join(root, "optoutgate"))
    _write(os.path.join(og, "sonar-project.properties"), "sonar.projectKey=optoutgate\n")
    _write(os.path.join(og, "package.json"),
           '{"name": "optoutgate", "scripts": {"test": "jest"}}\n')
    _write(os.path.join(og, ".sonar-optout"),
           "D-011: single-contributor repo, local scans only\n")
    _commit(og, "init")

    # diverged — bare remote + clone diverged to ahead 1 / behind 1, dirty file
    _build_diverged(root)

    return root


def _build_diverged(root):
    remote = os.path.join(root, "_remote.git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", remote],
                   check=True, env=GIT_ENV, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    work = os.path.join(root, "diverged")
    subprocess.run(["git", "clone", "-q", remote, work], check=True, env=GIT_ENV,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _write(os.path.join(work, "f"), "a\n")
    _commit(work, "c1")
    _git(work, "push", "-q", "origin", "master")
    _write(os.path.join(work, "f"), "a\nb\n")
    _commit(work, "c2")
    _git(work, "push", "-q", "origin", "master")
    _git(work, "reset", "-q", "--hard", "HEAD~1")   # back to c1 → behind 1
    _write(os.path.join(work, "f"), "a\nc\n")
    _commit(work, "c3")                              # diverge → ahead 1
    _git(work, "fetch", "-q", "origin")
    _write(os.path.join(work, "d.txt"), "dirty\n")  # untracked


# The bare remote ``_remote.git`` and any nested ``.git`` must not be discovered
# as repos; repodash only matches a ``.git`` *directory* inside a working tree,
# and a bare repo has none, so it is naturally skipped.
EXPECTED_REPO_NAMES = {
    "repoA", "repoB", "repoC", "my repo", "repoD", "sonarrepo", "diverged",
    "special", "jsnoonboard", "jsgateless", "jsgated", "optoutnoonboard",
    "optoutgate",
}


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    build(target)
    print(target)

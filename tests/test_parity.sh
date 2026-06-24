#!/usr/bin/env bash
# Standalone parity gate: build the shared fixtures, run both implementations'
# --json, and assert they are semantically identical. Intended for CI and for
# quick local checks without the Python test runner.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"

TREE="$(mktemp -d "${TMPDIR:-/tmp}/repodash-parity.XXXXXX")"
trap 'rm -rf "$TREE"' EXIT

python3 "$HERE/fixtures.py" "$TREE" >/dev/null

py_json="$(python3 "$ROOT/repodash.py" "$TREE" --json)"
sh_json="$(bash "$ROOT/repodash" "$TREE" --json)"

# Compare semantically: drop volatile fields, relativise paths, sort by name.
normalize() {
  python3 - "$1" <<'PY'
import json, os, sys
d = json.loads(sys.argv[1])
d["generated_at"] = "S"; d["base_dir"] = "B"
for r in d["repos"]:
    r["path"] = os.path.basename(r["path"].rstrip("/"))
d["repos"].sort(key=lambda r: r["name"])
print(json.dumps(d, sort_keys=True, indent=2))
PY
}

if diff <(normalize "$py_json") <(normalize "$sh_json") >/tmp/repodash_parity.diff 2>&1; then
  echo "PARITY OK — Python and bash --json are identical"
else
  echo "PARITY FAILED:"
  cat /tmp/repodash_parity.diff
  exit 1
fi

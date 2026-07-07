#!/usr/bin/env bash
# repodash coverage gate: bootstrap a venv with coverage.py, run the full test
# suite under it, and fail if covered-line percentage is below the threshold.
# One-time setup (requires network for pip install): just run this script.
# Subsequent runs are fully local.
set -euo pipefail

THRESHOLD="${REPODASH_COVERAGE_THRESHOLD:-80}"

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

venv_python="$repo_root/.venv/bin/python"
if command -v python3 >/dev/null 2>&1; then
  system_python="$(command -v python3)"
else
  system_python="$(command -v python)"
fi

bootstrap_venv() {
  if [ ! -x "$venv_python" ]; then
    echo "==> coverage: creating venv at $repo_root/.venv (one-time setup)"
    "$system_python" -m venv --system-site-packages "$repo_root/.venv"
  fi
  "$venv_python" -m pip install --quiet coverage 2>/dev/null || {
    echo "ERROR: pip install coverage failed. Check network connectivity."
    echo "       Or run manually: $venv_python -m pip install coverage"
    exit 1
  }
}

# Verify the venv python major.minor still matches the system — a Python upgrade
# can leave a venv with dangling symlinks that fail cryptically.
check_venv_healthy() {
  if ! "$venv_python" -c '' 2>/dev/null; then
    echo "==> coverage: venv python is broken (likely a system Python upgrade). Rebuilding."
    rm -rf "$repo_root/.venv"
    bootstrap_venv
  fi
}

# Ensure coverage is importable; re-run pip install if the package was deleted.
verify_coverage() {
  if ! "$venv_python" -c 'import coverage' 2>/dev/null; then
    echo "==> coverage: coverage.py not found in venv, installing"
    "$venv_python" -m pip install --quiet coverage 2>/dev/null || {
      echo "ERROR: pip install coverage failed."
      exit 1
    }
  fi
}

bootstrap_venv
check_venv_healthy
verify_coverage

echo "==> coverage: running test suite with coverage measurement"

"$venv_python" -m coverage run -m unittest discover tests -v

echo ""
echo "==> coverage: checking threshold (>= ${THRESHOLD}%)"

report="$("$venv_python" -m coverage report 2>&1)"
echo "$report"

if echo "$report" | grep -q '^TOTAL'; then
  pct=$(echo "$report" | awk '/^TOTAL/ {print $NF}' | tr -d '%')
  if [ "${pct%.*}" -lt "$THRESHOLD" ]; then
    echo ""
    echo "FAIL: coverage ${pct}% is below the ${THRESHOLD}% threshold."
    echo "Add tests to bring it up, or override with:"
    echo "  REPODASH_COVERAGE_THRESHOLD=${pct%.*} scripts/test_with_coverage.sh"
    exit 1
  fi
  echo "PASS: coverage ${pct}% >= ${THRESHOLD}%"
else
  echo "WARNING: could not parse coverage report TOTAL line."
  exit 1
fi

#!/usr/bin/env bash
# Points this clone's git hooks at the versioned hooks in scripts/git-hooks/
# so the pre-push test/parity/personal-data gate runs for every contributor.
# Run once per clone: bash scripts/install-hooks.sh
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

chmod +x scripts/git-hooks/pre-commit scripts/git-hooks/pre-push \
       scripts/git-hooks/scan-personal-data.sh scripts/test_with_coverage.sh
git config core.hooksPath scripts/git-hooks

echo "Installed: git config core.hooksPath -> scripts/git-hooks"
echo ""
echo "pre-commit will now run: test suite, bash syntax check"
echo "pre-push will now run:   coverage gate (>=80%), parity gate,"
echo "                          personal-data scan"
echo ""
echo "One-time setup (requires network): bash scripts/test_with_coverage.sh"

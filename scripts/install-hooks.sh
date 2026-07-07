#!/usr/bin/env bash
# Points this clone's git hooks at the versioned hooks in scripts/git-hooks/
# so the pre-push test/parity/personal-data gate runs for every contributor.
# Run once per clone: bash scripts/install-hooks.sh
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

chmod +x scripts/git-hooks/pre-push scripts/git-hooks/scan-personal-data.sh
git config core.hooksPath scripts/git-hooks

echo "Installed: git config core.hooksPath -> scripts/git-hooks"
echo "pre-push will now run the test suite, parity gate, and personal-data scan."

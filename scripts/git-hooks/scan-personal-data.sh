#!/usr/bin/env bash
# Scans the added lines of outgoing commits for secrets and machine-personal
# data (home directory paths, the pusher's own git email) before they leave
# the machine. Invoked by scripts/git-hooks/pre-push; safe to run standalone
# as: scripts/git-hooks/scan-personal-data.sh <remote_sha> <local_sha>
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

remote_sha="${1:?usage: scan-personal-data.sh <remote_sha> <local_sha>}"
local_sha="${2:?usage: scan-personal-data.sh <remote_sha> <local_sha>}"
zero_sha="0000000000000000000000000000000000000000"

if [ "$remote_sha" = "$zero_sha" ]; then
    # New branch/ref: diff against the empty tree so we still scan every
    # line the push introduces, not just what changed relative to a base.
    diff_range=("$(git hash-object -t tree /dev/null)" "$local_sha")
else
    diff_range=("$remote_sha" "$local_sha")
fi

# Verify the baseline SHA is reachable in this repo. A missing remote SHA
# means the local clone hasn't fetched it yet (the push may originate from a
# different worktree or the remote advanced independently). In that case
# fall back to diffing against the empty tree with a warning.
if [ "${diff_range[0]}" != "$zero_sha" ] \
   && ! git cat-file -e "${diff_range[0]}" 2>/dev/null; then
    echo "==> pre-push: WARNING: baseline SHA ${diff_range[0]} not found locally — diffing against empty tree" >&2
    diff_range=("$(git hash-object -t tree /dev/null)" "$local_sha")
fi

# The local SHA is what we're pushing — it must exist. If not, something
# is fundamentally wrong; skip the scan rather than crash.
if ! git cat-file -e "$local_sha" 2>/dev/null; then
    echo "==> pre-push: WARNING: local SHA $local_sha not found — skipping scan" >&2
    exit 0
fi

diff_output="$(git diff --unified=0 "${diff_range[@]}" -- . ':(exclude)scripts/git-hooks/**')"

# Only the lines a human is actually about to publish.
added_lines="$(printf '%s\n' "$diff_output" | grep -E '^\+[^+]' || true)"

if [ -z "$added_lines" ]; then
    exit 0
fi

fail=0
report() {
    # $1 = human label, $2 = matching lines
    if [ -n "$2" ]; then
        echo "  [!] $1:" >&2
        printf '%s\n' "$2" | sed 's/^/      /' >&2
        fail=1
    fi
}

# --- Secrets: high-confidence structural patterns -------------------------
report "Private key block" \
    "$(printf '%s\n' "$added_lines" | grep -E -- '-----BEGIN (RSA|EC|DSA|OPENSSH|PGP)? ?PRIVATE KEY-----' || true)"
report "AWS access key ID" \
    "$(printf '%s\n' "$added_lines" | grep -E -- 'AKIA[0-9A-Z]{16}' || true)"
report "GitHub token" \
    "$(printf '%s\n' "$added_lines" | grep -E -- 'gh[pousr]_[0-9A-Za-z]{36,}' || true)"
report "Slack token" \
    "$(printf '%s\n' "$added_lines" | grep -E -- 'xox[baprs]-[0-9A-Za-z-]{10,}' || true)"
report "Google API key" \
    "$(printf '%s\n' "$added_lines" | grep -E -- 'AIza[0-9A-Za-z_-]{35}' || true)"
report "JWT" \
    "$(printf '%s\n' "$added_lines" | grep -E -- 'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}' || true)"

# --- Personal data: this machine's own identifiers leaking into the repo --
home_dir="${HOME:-}"
if [ -n "$home_dir" ] && [ "$home_dir" != "/" ]; then
    report "Local \$HOME path ($home_dir)" \
        "$(printf '%s\n' "$added_lines" | grep -F -- "$home_dir" || true)"
fi

git_email="$(git config user.email 2>/dev/null || true)"
if [ -n "$git_email" ]; then
    report "Committer email ($git_email)" \
        "$(printf '%s\n' "$added_lines" | grep -F -- "$git_email" || true)"
fi

# Generic email addresses, excluding well-known non-personal patterns:
#   *@example.com / *@example.org      -- doc/test placeholders
#   *@*.anthropic.com, noreply@github  -- tool-generated trailers
#   git@github.com / git@gitlab.com / git@bitbucket.org -- SSH remote syntax
email_pattern='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
allowlist='@(example\.com|example\.org)|noreply@|@[a-z0-9.-]*anthropic\.com|^\+git@(github|gitlab|bitbucket)\.(com|org)'
other_emails="$(printf '%s\n' "$added_lines" \
    | grep -E -- "$email_pattern" \
    | grep -Ev -- "$allowlist" || true)"
report "Email address" "$other_emails"

if [ "$fail" -ne 0 ]; then
    echo "" >&2
    echo "pre-push: personal data / secrets guardrail failed (see above)." >&2
    echo "If a match is a false positive, fix the pattern in scripts/git-hooks/scan-personal-data.sh" >&2
    echo "or bypass deliberately with 'git push --no-verify'." >&2
    exit 1
fi

exit 0

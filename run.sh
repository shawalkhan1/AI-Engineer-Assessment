#!/usr/bin/env bash
# Backwards-compatible shell entrypoint. The portable launcher owns setup/cleanup.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1 && \
      "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    exec "$candidate" "$REPO_ROOT/run.py" "$@"
  fi
done
printf '%s\n' 'error: Python 3.11 or newer is required.' >&2
exit 2

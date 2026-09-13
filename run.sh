#!/usr/bin/env bash
#
# Aerlink disruption desk.
#
#   bash run.sh --cases cases --output artifacts/full-run
#   bash run.sh --inbound /abs/inbound.txt --case-id new-001 --output artifacts/new-001
#   bash run.sh --cases cases --dry-run --output artifacts/dry-run
#   bash run.sh --test
#
# Creates/reuses a project-local .venv, installs the pinned dependencies when they
# are missing, starts the supplied operations server only if nothing valid is already
# listening, and stops only a server this script itself started.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

VENV_DIR="${AERLINK_VENV:-$REPO_ROOT/.venv}"
HEALTH_TIMEOUT_S="${AERLINK_HEALTH_TIMEOUT_S:-30}"
SERVER_PID=""
SERVER_LOG="$REPO_ROOT/state/ops-server.log"

log()  { printf '%s\n' "$*" >&2; }
fail() { printf 'error: %s\n' "$*" >&2; exit 2; }

cleanup() {
  # Only ever stop a server this script started. Anything already running when we
  # arrived belongs to someone else.
  if [[ -n "$SERVER_PID" ]]; then
    log "stopping the operations server we started (pid $SERVER_PID)"
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# --- interpreter -------------------------------------------------------------

find_python() {
  for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        printf '%s' "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

BOOTSTRAP_PYTHON="$(find_python)" || fail \
  "no Python 3.11 or newer found on PATH. Install one and try again (developed and verified on 3.14.0)."

if [[ -x "$VENV_DIR/Scripts/python.exe" ]]; then
  VENV_PYTHON="$VENV_DIR/Scripts/python.exe"      # Windows layout
elif [[ -x "$VENV_DIR/bin/python" ]]; then
  VENV_PYTHON="$VENV_DIR/bin/python"
else
  log "creating a virtual environment in $VENV_DIR"
  "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR" || fail "could not create a virtual environment in $VENV_DIR"
  if [[ -x "$VENV_DIR/Scripts/python.exe" ]]; then
    VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
  else
    VENV_PYTHON="$VENV_DIR/bin/python"
  fi
fi

# --- dependencies ------------------------------------------------------------
# Installed only when something is actually missing, so a warm run does no network.

if ! "$VENV_PYTHON" -c 'import openai, pydantic, httpx, tiktoken, pytest' >/dev/null 2>&1; then
  log "installing pinned dependencies (requires network access this once)"
  "$VENV_PYTHON" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  "$VENV_PYTHON" -m pip install --quiet -r "$REPO_ROOT/requirements.txt" \
    || fail "dependency install failed. requirements.txt needs to be downloadable once; after that runs are offline apart from your own OpenAI calls."
fi

# --- test mode: no server, no key -------------------------------------------

for arg in "$@"; do
  if [[ "$arg" == "--test" ]]; then
    log "running the test suite (no operations server, no OpenAI key, no mutations)"
    exec "$VENV_PYTHON" -m pytest "$REPO_ROOT/tests"
  fi
done

# --- configuration -----------------------------------------------------------

if [[ ! -f "$REPO_ROOT/.env" ]]; then
  log "no .env found; creating one from .env.example"
  cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
  log "put the supplied OpenAI key in .env as OPENAI_API_KEY=... and run again"
  exit 2
fi

# Reports what is set, never what it is set to.
"$VENV_PYTHON" - <<'PY' || exit 2
import sys
sys.path.insert(0, ".")
from aerlink.config import ConfigError, load_config
try:
    config = load_config()
except ConfigError as exc:
    print("error: {}".format(exc), file=sys.stderr)
    raise SystemExit(2)
print("ops base url:  {}".format(config.ops_base_url), file=sys.stderr)
print("ops key:       set".format(), file=sys.stderr)
print("openai key:    {}".format("set" if config.openai_api_key else "MISSING"), file=sys.stderr)
print("openai model:  {}".format(config.openai_model), file=sys.stderr)
print("journal:       {}".format(config.journal_path), file=sys.stderr)
for note in config.notes:
    print("note:          {}".format(note), file=sys.stderr)
PY

OPS_BASE_URL="$("$VENV_PYTHON" -c 'import sys; sys.path.insert(0,"."); from aerlink.config import load_config; print(load_config(require_openai_key=False).ops_base_url)')"

# --- operations server -------------------------------------------------------

probe_service() {
  "$VENV_PYTHON" - "$OPS_BASE_URL" <<'PY'
import sys
import urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + "/health", timeout=3) as response:
        import json
        print(json.load(response).get("service", "unknown"))
except Exception:
    print("")
PY
}

SERVICE="$(probe_service)"
if [[ "$SERVICE" == "aerlink-ops" ]]; then
  log "operations server already running at $OPS_BASE_URL (not started by us, will not be stopped by us)"
elif [[ -n "$SERVICE" ]]; then
  fail "something is already listening at $OPS_BASE_URL but it reports itself as '$SERVICE', not the Aerlink operations API.
       Refusing to send payments and re-bookings to an unrecognised service, and refusing to kill it.
       Free the port, or start the supplied server elsewhere:
           OPS_PORT=9642 python3 env/ops_server.py
       and set OPS_BASE_URL in .env to match."
else
  log "starting the supplied operations server"
  mkdir -p "$REPO_ROOT/state"
  "$VENV_PYTHON" "$REPO_ROOT/env/ops_server.py" > "$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  deadline=$(( SECONDS + HEALTH_TIMEOUT_S ))
  until [[ "$(probe_service)" == "aerlink-ops" ]]; do
    if (( SECONDS >= deadline )); then
      log "--- last lines of $SERVER_LOG ---"
      tail -n 20 "$SERVER_LOG" >&2 || true
      fail "the operations server did not become healthy within ${HEALTH_TIMEOUT_S}s"
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      log "--- last lines of $SERVER_LOG ---"
      tail -n 20 "$SERVER_LOG" >&2 || true
      SERVER_PID=""
      fail "the operations server exited during start-up"
    fi
    sleep 0.5
  done
  log "operations server healthy at $OPS_BASE_URL (pid $SERVER_PID)"
fi

# --- run ---------------------------------------------------------------------

set +e
"$VENV_PYTHON" -m aerlink.cli "$@"
STATUS=$?
set -e
exit "$STATUS"

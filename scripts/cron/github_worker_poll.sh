#!/usr/bin/env sh
# Cron pre-run collector for the GitHub issue worker.
#
# Prints the eligible-issue report for the agent's prompt. Costs no AI usage on
# its own: if the report says eligible=0 the prompt tells the agent to stop, and
# the paired monitor script keeps the agent from starting at all when nothing
# changed.
#
# The GitHub token is read from the Hermes .env (mode 600) by the poller and is
# never printed here.

set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
HERMES_HOME_DIR="${HERMES_HOME:-/opt/data}"

PY="${PYTHON:-}"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    if [ -x /opt/hermes/.venv/bin/python ]; then
        PY=/opt/hermes/.venv/bin/python
    else
        PY=python3
    fi
fi

cd "$HERE" || exit 0
  
# Issue #45: a hermes-ready issue that cannot be used is reported once, on GitHub,
# with structured reasons (deduplicated by issue + body). This does not change the
# poll's stdout: the agent is still only woken when the fingerprint moves.
STEVEN_INTAKE_REJECT_COMMENTS="${STEVEN_INTAKE_REJECT_COMMENTS:-1}"
export STEVEN_INTAKE_REJECT_COMMENTS
  
# Issue #45 (Priority 2): publish the redacted worker/cron/budget status on a bounded
# cadence. Output goes to a log file, never to stdout, so monitor deduplication and
# the worker wake-up rule are untouched.
# Bounded cadence: at most one publish every 10 minutes (the external watchdog
# tolerates 15), so a 1-minute poll does not hammer the GitHub API.
if [ "${STEVEN_STATUS_PUBLISH:-1}" = "1" ]; then
    STATUS_DIR="$HERMES_HOME_DIR/ops/bridge"
    mkdir -p "$STATUS_DIR" 2>/dev/null || true
    if [ ! -f "$STATUS_DIR/last-publish" ] || [ -n "$(find "$STATUS_DIR/last-publish" -mmin +10 2>/dev/null)" ]; then
        "$PY" scripts/bridge_status_publish.py --live \
            >"$STATUS_DIR/publish.log" 2>&1 || true
        touch "$STATUS_DIR/last-publish" 2>/dev/null || true
    fi
fi
  
exec "$PY" scripts/issue_poller.py \
    --repo "StevenMarketing/steven-ai-os" \
    --owner "StevenMarketing" \
    --label "hermes-ready" \
    --token-file "$HERMES_HOME_DIR/.env" \
    --state "$HERMES_HOME_DIR/ops/github-worker/state.json"

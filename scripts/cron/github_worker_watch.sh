#!/usr/bin/env sh
# Cron change-detector for the GitHub issue worker.
#
# Prints ONE deterministic line: the fingerprint of the eligible issue set
# (plus the stale-claim marker). Identical output to the previous tick means the
# scheduler never starts the agent, so a 10-minute poll costs $0.00 in AI usage.
# It can only change when an eligible issue is opened, edited, unprocessed or
# when a claim expires -- never because of the clock.

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

exec "$PY" scripts/issue_poller.py \
    --repo "StevenMarketing/steven-ai-os" \
    --owner "StevenMarketing" \
    --label "hermes-ready" \
    --token-file "$HERMES_HOME_DIR/.env" \
    --state "$HERMES_HOME_DIR/ops/github-worker/state.json" \
    --watch

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

exec "$PY" scripts/issue_poller.py \
    --repo "StevenMarketing/steven-ai-os" \
    --owner "StevenMarketing" \
    --label "hermes-ready" \
    --token-file "$HERMES_HOME_DIR/.env" \
    --state "$HERMES_HOME_DIR/ops/github-worker/state.json"

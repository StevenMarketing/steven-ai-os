#!/bin/sh
# Private GReply preview container entrypoint -- FAIL CLOSED.
#
# The preview is fixtures-only and login-gated. This script refuses to start
# unless preview/guard.py approves the environment. It then binds the app to
# LOOPBACK ONLY; on the owner's VPS a reverse proxy (Caddy) on the same host is
# the only way in. There is deliberately no public port and no anonymous mode.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_ROOT=$(CDPATH= cd -- "$HERE/.." && pwd)
cd "$APP_ROOT"

echo "GReply PRIVATE PREVIEW -- DEMO (fixtures only; nothing is published to Google)"
echo "version: ${GBP_PREVIEW_VERSION:-unknown}  commit: ${GBP_PREVIEW_COMMIT:-unknown}"

if ! python3 preview/guard.py; then
    echo "refusing to start: preview guard rejected the environment" >&2
    exit 78  # EX_CONFIG
fi

PORT="${PORT:-8080}"
export PYTHONPATH="$APP_ROOT/src"

# Loopback only. scripts/serve_ui.py itself rejects a non-loopback host, so a
# misconfiguration can never expose the dashboard directly. A same-host reverse
# proxy (Caddy, see preview/Caddyfile.snippet) is what fronts it.
exec python3 scripts/serve_ui.py \
    --auth \
    --host 127.0.0.1 \
    --port "$PORT" \
    --db /tmp/gbp_preview.db \
    --reset

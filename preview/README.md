# Private GReply preview (DEMO)

A login-gated, fixtures-only preview of the GReply dashboard for the owner to
open on desktop and mobile. Nothing here is deployed by the repository, and
nothing here can reach Google.

- `Dockerfile` — the preview image (non-root, loopback-bound, health-checked).
- `guard.py` — fail-closed pre-flight: refuses to start on a missing/weak
  session secret, a missing demo password, an anonymous-mode request, or any
  live Google/production variable. **No value is ever echoed**, only names.
- `entrypoint.sh` — runs the guard, then serves the demo on `127.0.0.1` only.
- `fly.toml` — private Fly.io app config (no public IP, no `[http_service]`).

The demo image is not production: it seeds from `fixtures/` into a throwaway
`/tmp` database, is `DRAFT MODE` only, and has no publish endpoint.

Full runbook, options, costs and the approvals required before anything is
deployed: [`../docs/PREVIEW-DEPLOY.md`](../docs/PREVIEW-DEPLOY.md).

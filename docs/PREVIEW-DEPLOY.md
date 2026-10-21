# GReply private preview — deployment runbook

**Status: designed and wired, NOT deployed.** No host, domain, DNS record,
secret, port, firewall rule or hosting subscription exists for this yet, and
this runbook does **not** authorise any of them. Every deployment step below is
gated on an explicit owner approval (see [Approvals required](#approvals-required)).

Goal (issue #73): a stable private HTTPS link to the demo dashboard that the
owner can open on desktop and mobile, with no Python/Git/PowerShell locally, and
no anonymous exposure of the dashboard or any customer data.

## What ships in this increment

| Artifact | Purpose |
|---|---|
| `preview/Dockerfile` | Preview image: non-root, stdlib-only, health-checked. |
| `preview/guard.py` | Fail-closed pre-flight; refuses an unsafe environment. |
| `preview/entrypoint.sh` | Runs the guard, then binds the app to loopback only. |
| `preview/fly.toml` | Private Fly.io app config (no public IP, no `[http_service]`). |
| `tests/test_preview_deploy.py` | Offline guard + config + auth-denial tests. |

Nothing in this increment deploys, calls Google, publishes a reply, opens a
port, changes DNS, rotates a credential or spends money.

## Safety invariants (enforced, not aspirational)

1. **Fixtures only.** The demo seeds from `fixtures/` into a throwaway
   `/tmp/gbp_preview.db`. No live Google OAuth, reviews, credentials, or
   publishing. `guard.py` refuses to start if any live-Google/production
   variable is present.
2. **Login is the only way in.** `--auth` is mandatory in the entrypoint; the
   guard refuses a missing/weak `GBP_SESSION_SECRET` (< 32 chars), a missing
   `GBP_DEMO_PASSWORD`, and any `GBP_ALLOW_ANON` setting.
3. **No public port.** The app binds `127.0.0.1` only (`scripts/serve_ui.py`
   refuses any other host) and `preview/fly.toml` declares no public
   `[[services.ports]]` and no `[http_service]`. Access is over the platform's
   private network only.
4. **Isolated.** The preview runs from its own image and its own ephemeral DB;
   it never touches the production checkout, worker, bridge, cron, or budgets.
5. **DEMO is visible.** The dashboard already renders the `DRAFT MODE` banner;
   the container logs the image version and commit on start
   (`GBP_PREVIEW_VERSION` / `GBP_PREVIEW_COMMIT`).
6. **No invented link.** The exact URL is reported only after it is deployed and
   verified reachable. Until then there is no URL.

## Options (choose one)

### Option A — Fly.io private app (recommended)

Private app, no public IP; the owner reaches it via `fly proxy` (or the org
WireGuard tunnel). Cheapest, least moving parts, one extra hop for the owner.

```sh
# after approval only
fly apps create greply-preview --org <owner-org>
fly secrets set --app greply-preview GBP_SESSION_SECRET=<32+ chars> GBP_DEMO_PASSWORD=<strong>
fly deploy --app greply-preview --config preview/fly.toml
# owner access (no public URL is created):
fly proxy 8080:8080 --app greply-preview      # then open http://127.0.0.1:8080
```

### Option B — Cloudflare Tunnel + Access

A `cloudflared` sidecar in the same pod tunnels `127.0.0.1:8080` out; Cloudflare
Access gates it by the owner's email, giving a stable private HTTPS URL.
Requires a Cloudflare account and a domain already under its control.

```sh
# after approval only
# cloudflared tunnel create greply-preview && cloudflared tunnel route dns ...
# then run the tunnel container with: --url http://127.0.0.1:8080
```

## Costs (order of magnitude — confirm on the provider's own pricing page)

| Option | Idle cost | Notes |
|---|---|---|
| A — Fly.io private, 1 shared-cpu-1x / 256 MB | ~$2–3 / month if the machine is always on; $0 if auto-stopped | No public IP. |
| B — Cloudflare Tunnel | $0 tunnel; Access free tier covers a small team | Needs a domain already on Cloudflare. |

Either option is inside a few dollars a month; both require the owner's card on
file with the provider.

## Health check, logs, rollback

- **Health:** `preview/fly.toml` checks `GET /` (the login page) over the private
  network every 30 s; a 200 proves the app booted with auth on. The image also
  carries a `HEALTHCHECK` for local runs.
- **Logs:** the app's access log contains request lines only — no secrets, and
  `guard.py` prints variable **names**, never values. Never hand-copy a secret
  into a log or an issue.
- **Rollback:** `fly deploy --image <previous-image-tag>` (or delete the app).
  The image is stateless apart from the throwaway `/tmp` DB, so rollback is a
  redeploy of the previous image; no migration or data restore is involved.

## Approvals required

This issue explicitly withholds these; a human must decide each before any
deploy:

1. **Hosting account / capacity** — which provider, and that the free capacity
   actually exists (do not assume it).
2. **Spending** — approving the provider's card and the monthly cost above.
3. **Secrets** — generating and setting `GBP_SESSION_SECRET` and
   `GBP_DEMO_PASSWORD` (never committed, never pasted into an issue or a chat).
4. **Domain / DNS** — only for Option B (Route a hostname through
   Cloudflare Access).
5. **Network** — that reaching the preview via `fly proxy` / WireGuard (Option
   A) or Cloudflare Access (Option B) is acceptable, i.e. no public port is
   opened.

Until all five are answered, this stays a Draft PR and no URL is reported.

## Non-goals

- Not a production deployment and not a substitute for the local dev UI.
- Not a bridge command and not an authorisation to deploy.
- Does not block GReply Increment 1 (#71 / PR #72).

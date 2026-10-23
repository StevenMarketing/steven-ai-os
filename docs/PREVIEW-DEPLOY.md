# GReply private preview — deployment runbook (owner VPS)

**Status: designed and wired, NOT deployed.** No host, domain, DNS record,
secret, port, firewall rule or hosting subscription exists for this yet, and
this runbook does **not** authorise any of them. Every deployment step below is
gated on an explicit owner approval (see [Approvals required](#approvals-required)).

Goal (issue #73): a stable private HTTPS link to the demo dashboard that the
owner can open on desktop and mobile, with no Python/Git/PowerShell locally, and
no anonymous exposure of the dashboard or any customer data.

This runbook targets the **owner's own VPS**. It supersedes an earlier
Fly.io-based draft (issue #73 / PR #75) that was never owner-approved and must
not be deployed.

## What ships in this increment

| Artifact | Purpose |
|---|---|
| `preview/Dockerfile` | Preview image: non-root, stdlib-only, health-checked. |
| `preview/guard.py` | Fail-closed pre-flight; refuses an unsafe environment. |
| `preview/entrypoint.sh` | Runs the guard, then binds the app to loopback only. |
| `preview/steven-preview.service` | systemd unit that runs the container from the server-side env file, loopback only. |
| `preview/Caddyfile.snippet` | Caddy site block that terminates HTTPS at `preview.adzdigital.com` and proxies to loopback. |
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
3. **No public port.** The container publishes `127.0.0.1:8080` **only**
   (`scripts/serve_ui.py` refuses any non-loopback host), and
   `preview/steven-preview.service` binds nothing else. Caddy on the same host
   is the only thing that reaches it.
4. **Isolated.** The preview runs from its own image, its own ephemeral DB and
   its own systemd unit; it never touches the production checkout, worker,
   bridge, cron, or budgets.
5. **Secrets stay on the server.** `GBP_SESSION_SECRET` and `GBP_DEMO_PASSWORD`
   live in a mode-0600 env file generated on the VPS, outside the repository.
   No secret is ever committed, or pasted into an issue or a chat.
6. **DEMO is visible.** The dashboard already renders the `DRAFT MODE` banner;
   the container logs the image version and commit on start
   (`GBP_PREVIEW_VERSION` / `GBP_PREVIEW_COMMIT`).
7. **No invented link.** The exact URL is reported only after it is deployed and
   verified reachable. Until then there is no URL.

## Options (choose one)

### Option A — container + systemd + Caddy on the owner's VPS (recommended)

Build the existing preview image **on the VPS**, run it under the
`steven-preview` systemd unit bound to loopback, and let Caddy terminate HTTPS
at `preview.adzdigital.com`.

```sh
# On the VPS, after approval only. Run from the repository checkout.

# 1. Build the image (nothing is pushed anywhere).
docker build -f preview/Dockerfile -t greply-preview:latest .

# 2. Generate the secrets ON THE SERVER and write the mode-0600 env file.
sudo install -d -m 0755 /etc/steven-preview
sudo sh -c 'umask 077; {
  echo "GBP_PREVIEW=1"
  echo "GBP_SESSION_SECRET=$(openssl rand -hex 32)"
  echo "GBP_DEMO_PASSWORD=$(openssl rand -base64 24)"
} > /etc/steven-preview/preview.env'
sudo chmod 0600 /etc/steven-preview/preview.env   # confirm: never group/other readable

# 3. Install and start the systemd unit (loopback only, no public port).
sudo install -m 0644 preview/steven-preview.service /etc/systemd/system/steven-preview.service
sudo systemctl daemon-reload
sudo systemctl enable --now steven-preview
systemctl status steven-preview --no-pager      # active (running)

# 4. Point Caddy at loopback for the hostname (automatic HTTPS).
#    Append preview/Caddyfile.snippet to /etc/caddy/Caddyfile, then:
sudo systemctl reload caddy
```

`preview.adzdigital.com` must already resolve to the VPS (A/AAAA record)
**before** Caddy is reloaded, or the automatic certificate request fails.

### Option B — same container behind Cloudflare Tunnel + Access

Instead of Caddy, run a `cloudflared` tunnel that forwards to
`http://127.0.0.1:8080` and gate it with Cloudflare Access by the owner's email.
Requires a Cloudflare account and a domain already under its control. The
systemd unit and the loopback binding are unchanged; only the front door
differs.

## Costs (order of magnitude — confirm on the provider's own pricing page)

| Option | Idle cost | Notes |
|---|---|---|
| A — owner's VPS (existing host) | $0 extra if the VPS already runs and has spare capacity; a small VPS is a few dollars a month | Caddy and the container share the host; no managed platform fee. |
| B — Cloudflare Tunnel | $0 tunnel; Access free tier covers a small team | Needs a domain already on Cloudflare. |

Neither option adds a managed-hosting subscription on top of the VPS.

## Health check, logs, rollback

- **Health:** the image carries a `HEALTHCHECK` that fetches `GET /` (the login
  page) on loopback; a 200 proves the app booted with auth on. Check the unit
  with `systemctl status steven-preview` and the container with
  `docker inspect --format '{{.State.Health.Status}}' steven-preview`.
- **Logs:** `journalctl -u steven-preview` shows container output; the app's
  access log contains request lines only — no secrets, and `guard.py` prints
  variable **names**, never values. Never hand-copy a secret into a log or an
  issue.
- **Rollback:** `sudo systemctl stop steven-preview`, re-point the image tag (or
  rebuild the previous commit's image) and `sudo systemctl start
  steven-preview`. The image is stateless apart from the throwaway `/tmp` DB, so
  rollback is a restart of the previous image; no migration or data restore is
  involved. To disable entirely: `sudo systemctl disable --now steven-preview`
  and remove the Caddy snippet.

## Approvals required

This issue explicitly withholds these; a human must decide each before any
deploy:

1. **Hosting capacity** — that the owner's VPS has spare CPU/memory/disk and may
   run one more always-on container (do not assume it does).
2. **Spending** — approving any additional VPS capacity or provider cost above.
3. **Secrets** — generating `GBP_SESSION_SECRET` and `GBP_DEMO_PASSWORD` on the
   server (never committed, never pasted into an issue or a chat).
4. **Domain / DNS** — that `preview.adzdigital.com` may be pointed at the VPS
   (A/AAAA record), which is required for Caddy's automatic HTTPS.
5. **Network** — that opening 443 only through Caddy, with the container bound
   to `127.0.0.1:8080` and no other inbound port, is acceptable.

Until all five are answered, this stays a Draft PR and no URL is reported.

## Non-goals

- Not a production deployment and not a substitute for the local dev UI.
- Not a bridge command and not an authorisation to deploy.
- Does not change anything on any server; this repository only carries the unit,
  the Caddy snippet and this runbook.
- Does not block GReply Increment 1 (#71 / PR #72).

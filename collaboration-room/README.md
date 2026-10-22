# Steven AI OS — isolated collaboration prototype

Status 2026-10-22: implemented and tested locally; **NOT deployed**. No production,
Hermes, GitHub write, OpenAI or Muse connector is enabled. Only the explicitly
labelled simulator executes transitions. No API/model cost is incurred.

## Audit evidence and boundaries

- Main checkout `86831b3f3dbf8cc7c7f351c243d8352499df8ecb` (merged #84).
- Open PR inventory: #66, #68, #70, #72, #75. GReply #72 and preview #75 remain untouched.
- Existing #74 control plane / #65 intake remain authoritative for live execution;
  this prototype adds no competing production queue or worker. #81/#82/#83 are
  represented in main; VPS deployment has not been independently rechecked here.
- GitHub status #46 latest observed generated_at 2026-10-22T03:10:03Z, cron last
  run 03:09:04Z `ok`, failure streak 0, worker idle / claims 0. At audit time
  03:18:08Z this was 485 seconds old, within the declared 900-second window.
  This is publisher evidence, not a direct console/host-health check.
- No Hostinger terminal is shared in this browser session. VPS SHA, uptime,
  current resource use and port 32769 are NOT freshly verified. Preserve 32769.
- Botkeep home and sign-in opened successfully. Account is not signed in.
  Founder Free quota (5 slots / 2048 MB / 150% / 2048 MB) remains owner-reported.
- Public official docs advertise Node.js, persistent volume, environment secrets,
  GitHub branch/project-root selection and ZIP import. Exact runtime versions,
  mount path, backup restore, HTTPS/public routing, SSE buffering, outbound network
  and account limits require authenticated verification. Do not treat docs as
  successful deployment evidence.
- Browser auto-review rejected read-only login-field inspection, citing Discord
  authentication authority despite no Discord selection. No workaround attempted.

## Design and deployment gates

One Node.js 24 process, no npm dependencies, SQLite WAL + FULL synchronization.
Target initial reservation (proposal only): one slot, 512 MB RAM, 50% CPU,
512 MB storage, leaving headroom in the stated Founder allocation. Measure actual
RSS/disk before enlarging. No services/resources have been created.

SQLite is suitable for this single-process, low-volume disposable prototype ONLY
if Botkeep supplies a local persistent disk with reliable file locking. Never
put it on an unverified network filesystem. PostgreSQL is preferred if multiple
instances or shared access are needed, but consumes an additional slot and RAM;
verify offered minimums, private connectivity, backups and cost before creating.
If Botkeep fails durability/security checks, propose an isolated service on the
existing VPS; this requires production-host approval, not an automatic fallback.

### Start (only in a separate test environment)

Root directory: `collaboration-room`; start: `npm start`; runtime: Node >=24.
No install step is required. Set through the host secret/environment facility:

- `ROOM_OWNER_TOKEN`: random owner-only bearer credential, >=32 characters.
  Never place it in chat, URLs, logs, Git, screenshots, test reports or ZIPs.
- `ROOM_DB_PATH`: absolute persistent-volume file path, outside source tree.
- `ROOM_ORIGIN`: exact HTTPS origin of the test app.
- `HOST`: 0.0.0.0 only behind the verified HTTPS host proxy; local default 127.0.0.1.
- `PORT`: host-assigned application port.

Do not expose without HTTPS and access token configured. `/` is a public login
shell and `/healthz` reveals only readiness; all state and mutations require
bearer authorization. Browser token is memory-only. No cross-origin access is
allowed. The prototype has only one human owner role; agent credentials and
multi-user RBAC are intentionally absent. Sensitive actions have no execution
or approval endpoint: risk-marked tasks remain awaiting_approval. Do not use
this as a security-reviewed production gateway.

Before deploy, verify quota and zero price, Node 24, persistent mount, environment
secret handling and HTTPS. Prefer clean ZIP of this directory if GitHub import
would require granting broader repository access; creating new GitHub App access
needs separate approval. Never upload the repository root or credentials.

After deploy: unauthorized API returns 401; wrong Origin returns 403; create a
sample message/task; reload; stop/start/redeploy the test workload; verify both
records and audit survive, then run simulator. Test SSE reconnect and access from
mobile, disk-full behavior, backup restore and startup recovery. Record exact URL,
SHA, timestamps and observed resource use. Platform persistence remains UNVERIFIED
until these live checks pass. Backend tests alone do not establish 24/7 readiness.

## Lifecycle and concurrency

Prototype: queued -> running -> testing -> review_ready; risk-marked creation ->
awaiting_approval. Real agent assignments are visible but cannot be claimed.
SQLite BEGIN IMMEDIATE serializes claims; each attempt gets a random fencing
identifier. Heartbeats renew 60-second leases; old attempts cannot update after
lease expiry or a new claim. Expiry requeues up to three attempts then blocks.
Expiry runs at startup and every 10 seconds. Simulator completes immediately
and never invokes commands, models or an external worker. SSE sends fresh durable
snapshots; UI polls every 5 seconds and reconnects automatically. No automatic
external handoff or production completion is claimed.

## Integration design — disabled until separate approval

Prefer Botkeep -> GitHub existing intake -> VPS existing Bridge/worker -> linked
PR/evidence -> room projection. Room task IDs map uniquely to existing Issue IDs;
room lease is NOT authority for Hermes. Durable outbox + delivery receipts and
unique idempotency keys must be added before real delivery. Ambiguous delivery
stays UNKNOWN and must be reconciled before retry. Preserve existing actor checks,
TTL, replay markers, budgets, claim authority and protected-path controls.
No arbitrary-shell HTTP endpoint, no direct production DB access, no public Bridge.

ChatGPT UI is the interactive coordinator, not a persistent daemon. A future
OpenAI API connector is a separate identity requiring approved API access, model,
budget and data policy; it does not import this chat/session automatically.
Official Responses background mode exists, but does not itself supply a 24/7
orchestrator. Never display it as this ChatGPT conversation.

Official Meta documentation now describes Meta Model API and Muse Code CLI.
This does NOT prove that the owner's current Muse account/session can join a
shared room or accepts inbound tasks. Verify exact product, account access,
official API/CLI automation contract, permissions and cost. Until then show
not_connected; do not scrape or automate Muse's consumer UI. No assertion that
Muse has no API is made.

24/7 technical execution stays on VPS. Botkeep can provide durable UI/intake
without the owner's PC; availability, watchdog and failure recovery must be
proven separately. Model spend remains zero for this prototype. Future API usage,
Hermes budgets, production integration and deployment require explicit approval.

## Verification and rollback

`npm test` covers file persistence/reopen, conflicting duplicate requests,
concurrent connections/claim exclusion, expired claim fencing, three-attempt cap,
risk/real-agent execution denial, invalid transitions, authentication/origin,
input bounds, unknown fields, simulator result and SSE reconnect.

Current limitations: no visual/browser QA, platform restart test, live backup,
rate/quota enforcement, multi-user authorization, durable external outbox or
security assessment. Do not put real customer data here. Audit is transactional
and append-only through the API, not tamper-proof against infrastructure admins.

Rollback: stop ONLY the prototype workload, retain/export its test DB and restore
its previous source revision. No production changes or data migrations exist.
Do not delete database files or an existing workload. Do not merge this PR or
connect to production without the owner's separate approval.

Official references checked 2026-10-22:
- https://botkeep.cloud/docs/nodejs
- https://botkeep.cloud/docs/github
- https://botkeep.cloud/faq
- https://developers.openai.com/api/docs/guides/background
- https://dev.meta.ai/docs/overview

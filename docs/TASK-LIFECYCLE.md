# Task lifecycle (offline control plane — increment 1, issue #76)

`gbp.bridge.task_lifecycle` is a **durable, offline** reducer for ChatGPT-to-Hermes
task coordination. It is the first bounded increment of the control-plane work in
issue #74. It is an **inert library**: nothing in it executes a command, dispatches
work, sends a notification, opens a socket, or talks to a scheduler. A VPS
coordinator may drive it later; today it only turns *events as data* into state and
recommends a next safe action.

## What it does

* Persists task/request correlation, immutable event IDs and UTC timestamps in a
  standard-library SQLite store (`TaskLifecycleStore`).
* Reduces ordered lifecycle transitions through a fixed vocabulary:
  `queued → claimed → running → testing → review_ready → done`, with `blocked` as a
  sideways park and `failed` reachable from any non-terminal state.
* Separates **bridge queue acceptance** (`queued`) from an actual **worker claim**
  (`claimed` and beyond). The two are never conflated.
* Evaluates freshness from an **independent supplied clock** and an **explicitly
  supplied heartbeat**. A missing/expired/future heartbeat is `unknown`/`stale`,
  never `fresh`, and a task is never reported `done` off a stale heartbeat.
  `state.updated_at` is bookkeeping, **not** a heartbeat: omitting the heartbeat is
  always `unknown`.
* Stores and exposes **canonical** safe GitHub issue/PR evidence links, refusing
  malformed ones (query strings, fragments, control characters, ambiguous paths,
  zero/leading-zero/non-ASCII ids, IDs longer than 20 digits, non-HTTPS or non-GitHub hosts).
* Redacts every free-text field with `gbp.security.redact.redact_text` before it is
  stored or read back. Events are treated strictly as data.

## Guarantees

| Concern | Behaviour |
|---|---|
| Duplicate event id, same content | idempotent (`duplicate`, state unchanged) |
| Duplicate event id, different content | rejected (`conflicting_duplicate`) |
| Out-of-order event (older event time or a regressing edge) | recorded, state **not** moved and `updated_at`/freshness **not** refreshed (`out_of_order`) |
| Illegal forward edge | refused (`invalid_transition`) |
| First event is not `queued` | refused (`invalid_initial_state`) |
| `queued` event without a `request_id` | refused (`missing_request_id`) |
| Later event with a conflicting `request_id` | refused (`conflicting_request_id`) |
| `done` without evidence | refused (`completion_requires_evidence`) |
| Malformed payload, bad identifier, unknown field/evidence key | refused (`malformed_event`) |
| Non-finite / enormous / boolean timestamp | refused (`malformed_event`) |
| Missing/expired/future heartbeat | `unknown` / `stale` |
| Boolean / string / NaN / infinite / enormous TTL, invalid or missing freshness `now` | `unknown` (never raises) |
| Invalid explicit `append_event(now=...)` | refused (`invalid_now`), no write |
| Freshness requested for an unknown task | `unknown` (`unknown_task`), even with a fresh heartbeat |
| Restart / reopen | state, dedupe set and request correlation survive (SQLite file) |

### Provenance and correlation

A task opens with a `queued` event that carries its `request_id`; an initial event
in any other state (including `done` with a link) is refused, so no event can bypass
the lifecycle. Later events may **omit** the request id, but a **conflicting** value
is refused. The correlation is retained across a reopen.

### Event-time ordering

Ordering uses the persisted **event** timestamp, kept distinct from `recorded_at`.
A delayed event whose timestamp is older than the state's current event time is
recorded but cannot move the state or refresh `updated_at`/freshness. Newer
`blocked` and resume events remain valid, and the allowed `blocked → running` edge
is checked **before** the rank test, so a legitimate resume always wins. Terminal
states (`done`/`failed`) never reopen. UTC conversion overflow and enormous numeric
inputs are refused without leaking built-in exceptions; canonical timestamps retain
four-digit years so early dates round-trip correctly.

`next_safe_action(status)` returns a **recommendation string only** — for example
`await_worker_claim`, `begin_run`, `await_human_review`, `no_action_terminal`,
`refuse_unknown_state`. It never performs the action.

## Example offline snapshot

```python
from gbp.bridge.task_lifecycle import TaskLifecycleStore

with TaskLifecycleStore("tasks.sqlite") as store:
    store.append_event({"event_id": "e1", "task_id": "t1", "status": "queued",
                        "request_id": "req-76", "timestamp": "2026-10-21T09:00:00Z"})
    store.append_event({"event_id": "e2", "task_id": "t1", "status": "claimed",
                        "timestamp": "2026-10-21T09:00:30Z"})
    print(store.snapshot())
    # {'tasks': {'t1': {'status': 'claimed', 'next_safe_action': 'begin_run'}},
    #  'task_count': 1, 'event_count': 2}
```

Freshness must be asked with an explicit clock and heartbeat:

```python
verdict = store.task_freshness("t1", now="2026-10-21T09:05:00Z",
                               ttl_seconds=300, heartbeat="2026-10-21T09:04:30Z")
# {'state': 'fresh', ...}
store.task_freshness("t1", now="2026-10-21T09:05:00Z", ttl_seconds=300)
# {'state': 'unknown', 'reason': 'missing_heartbeat', ...}   # never 'fresh'
```

## Limitations (this increment)

* No execution, dispatch, notification, network call or scheduler integration.
* No automatic transition source: a caller must supply each event.
* Single-process SQLite; concurrency beyond the store's own connection is out of
  scope for increment 1.

## Rollback

Close the Draft PR. The module is not wired into any running service, so there is
no runtime activation to undo.

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
* Evaluates freshness from an **independent supplied clock** and a supplied
  heartbeat. A missing/expired heartbeat is `unknown`/`stale`, never `fresh`, and a
  task is never reported `done` off a stale heartbeat.
* Stores and exposes safe GitHub issue/PR evidence links, rejecting malformed ones.
* Redacts every free-text field with `gbp.security.redact.redact_text` before it is
  stored or read back. Events are treated strictly as data.

## Guarantees

| Concern | Behaviour |
|---|---|
| Duplicate event id, same content | idempotent (`duplicate`, state unchanged) |
| Duplicate event id, different content | rejected (`conflicting_duplicate`) |
| Out-of-order event (would regress) | recorded, state **not** regressed (`out_of_order`) |
| Illegal forward edge | refused (`invalid_transition`) |
| `done` without evidence | refused (`completion_requires_evidence`) |
| Malformed payload / unknown status | refused (`malformed_event`) |
| Missing/expired/future heartbeat | `unknown` / `stale` |
| Restart / reopen | state and dedupe set survive (SQLite file) |

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

## Limitations (this increment)

* No execution, dispatch, notification, network call or scheduler integration.
* No automatic transition source: a caller must supply each event.
* Single-process SQLite; concurrency beyond the store's own connection is out of
  scope for increment 1.

## Rollback

Close the Draft PR. The module is not wired into any running service, so there is
no runtime activation to undo.

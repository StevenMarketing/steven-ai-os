"""Cổng hợp nhất cho GitHub issue worker: ngân sách provider-aware + gate issue.

Vì sao cần module này
---------------------
`StateStore.can_start()` (trong `issue_worker_lib.py`) chỉ đếm **sổ cái riêng của
worker** ($0.06/issue, $0.15/ngày, 3 issue/ngày). Nó **không** thấy chi tiêu của các
phiên Hermes khác trên cùng một khoá OpenRouter, và trong code **không có trần retry**
(`MAX_ATTEMPTS` không tồn tại ở đâu). `scripts/budget.py` có cả hai thứ đó nhưng trước
đây **không được worker gọi**.

Module này bọc cả hai lớp, để worker chỉ cần gọi **một** lệnh cho mỗi bước::

    python3 scripts/worker_gate.py can-start --issue 42 --estimate 0.05
    python3 scripts/worker_gate.py claim     --issue 42 --estimate 0.05
    python3 scripts/worker_gate.py finish    --issue 42 --outcome done --cost 0.0086 --pr URL
    python3 scripts/worker_gate.py attempts  --issue 42
    python3 scripts/worker_gate.py report

Thứ tự cưỡng chế

1. **Ngân sách** (`budget.BudgetGuard`): trần $3/ngày theo
   `max(provider usage_daily, sổ cái)`, hạn mức khoá provider, **fail-closed** khi không
   đọc được số provider, và **trần retry** theo task (`max_attempts`).
2. **Gate issue** (`issue_worker_lib.StateStore`): hạn mức riêng của worker, chống chạy
   trùng (idempotency), claim nguyên tử + TTL, kill switch.

`claim` **giữ chỗ** ngân sách trước rồi mới claim; nếu claim thất bại thì **nhả chỗ**
ngay (không để rò ngân sách). `finish` nhả chỗ, ghi chi phí thật (tính vào trần retry)
và gửi thông báo Telegram theo kết cục.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import budget               # noqa: E402
import issue_worker_lib as lib  # noqa: E402

DEFAULT_ESTIMATE = 0.05
DEFAULT_TTL = lib.CLAIM_TTL_SECONDS

OUTCOME_EVENT = {
    "done": "task_done",
    "failed": "task_failed",
    "blocked": "task_blocked",
    "needs-info": "task_blocked",
}
FAILED_OUTCOMES = {"failed", "blocked", "needs-info"}


def task_key(issue: Any) -> str:
    return f"issue-{issue}"


class Gate:
    """Hai lớp cưỡng chế, một cửa vào."""

    def __init__(self, state_dir: Optional[str] = None,
                 budget_dir: Optional[str] = None,
                 now: Optional[float] = None) -> None:
        # `--state` theo cùng quy ước với issue_guard.py: đường dẫn tệp sổ cái của worker.
        self.state_path = state_dir or lib.default_state_dir()
        self.state = lib.StateStore(self.state_path)
        self.budget_dir = budget_dir or budget.default_state_dir()
        self.now = now

    # -- lớp ngân sách -----------------------------------------------------
    def guard(self) -> budget.BudgetGuard:
        return budget.BudgetGuard(self.budget_dir, now=self.now)

    def budget_check(self, issue: Any, estimate: float, provider: Optional[Dict[str, Any]] = None,
                     fail_open: Optional[bool] = None) -> Tuple[bool, str, Dict[str, Any]]:
        guard = self.guard()
        info = provider if provider is not None else budget.provider_usage()
        allowed, reason = guard.can_spend(estimate, task_key(issue), info, fail_open)
        return allowed, reason, guard.spend_today(info)

    # -- lệnh ---------------------------------------------------------------
    def can_start(self, issue: Any, estimate: float = DEFAULT_ESTIMATE,
                  provider: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # Đọc lại trạng thái trên đĩa: một tiến trình sống lâu (hoặc test dùng lại cùng
        # một Gate) không được kết luận dựa trên ảnh chụp cũ của sổ cái.
        self.state = lib.StateStore(self.state_path)
        ok_budget, budget_reason, spend = self.budget_check(issue, estimate, provider)
        ok_issue, issue_reason = self.state.can_start(issue, estimate)
        allowed = bool(ok_budget and ok_issue)
        return {
            "issue": issue,
            "estimate": estimate,
            "allowed": allowed,
            "budget_layer": {"allowed": ok_budget, "reason": budget_reason,
                             "spend_today_usd": spend["spend"], "basis": spend["basis"],
                             "provider_ok": spend["provider_ok"]},
            "issue_layer": {"allowed": ok_issue, "reason": issue_reason},
            "reason": "ok" if allowed else (budget_reason if not ok_budget else issue_reason),
        }

    def claim(self, issue: Any, estimate: float = DEFAULT_ESTIMATE, ttl: int = DEFAULT_TTL,
              provider: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        verdict = self.can_start(issue, estimate, provider)
        if not verdict["allowed"]:
            return {**verdict, "claimed": False}
        guard = self.guard()
        held, hold_reason = guard.reserve(task_key(issue), estimate, provider=provider)
        if not held:
            # hai tiến trình cùng qua can_start: chỉ một giữ được chỗ
            return {**verdict, "allowed": False, "claimed": False, "reason": hold_reason}
        try:
            run_id = self.state.claim(issue, ttl=ttl)
        except (lib.ClaimError, lib.LockTimeout) as exc:
            freed = guard.release(task_key(issue))
            return {**verdict, "allowed": False, "claimed": False,
                    "reason": f"{type(exc).__name__}: {exc}", "hold_released_usd": freed}
        self.state.append_audit({"event": "claim", "issue": issue, "run_id": run_id,
                                 "estimate": estimate, "gate": "worker_gate"})
        return {**verdict, "claimed": True, "run_id": run_id, "ttl": ttl,
                "reason": "ok", "hold_usd": estimate}

    def finish(self, issue: Any, outcome: str, cost: float = 0.0,
               pr: Optional[str] = None, note: str = "", tests: Optional[str] = None,
               notify: bool = True, detail: Optional[str] = None) -> Dict[str, Any]:
        if outcome not in OUTCOME_EVENT:
            raise SystemExit(f"kết cục không hợp lệ: {outcome!r} "
                             f"(dùng: {', '.join(sorted(OUTCOME_EVENT))})")
        guard = self.guard()
        task = task_key(issue)
        freed = guard.release(task)                       # nhả chỗ đã giữ (nếu còn)
        recorded = guard.record(task, cost, note=note or f"outcome={outcome}",
                                kind="attempt")
        store = lib.StateStore(self.state_path)
        with store.transaction():
            store._release_unlocked(str(issue))
            store.mark_processed(issue, outcome, pr=pr, cost=cost, note=note)
            usage = store.record_usage(cost)
            store.append_audit({"event": "finish", "issue": issue, "outcome": outcome,
                                "pr": pr, "cost": cost, "usage": usage, "note": note,
                                "gate": "worker_gate"})
        result: Dict[str, Any] = {
            "issue": issue, "outcome": outcome, "cost": cost, "pr": pr,
            "hold_released_usd": freed, "recorded": recorded,
            "usage_today": usage, "remaining": store.remaining_today(),
            "attempts": guard.attempts(task), "retry_cap": guard.max_attempts,
            "notified": None,
        }
        if notify:
            result["notified"] = self.notify(outcome, issue, pr=pr, tests=tests, detail=detail,
                                             cost=cost)
        return result

    def notify(self, outcome: str, issue: Any, pr: Optional[str] = None,
               tests: Optional[str] = None, detail: Optional[str] = None,
               cost: float = 0.0) -> Dict[str, Any]:
        """Thông báo Telegram theo kết cục; lỗi thông báo không làm hỏng finish."""
        event = OUTCOME_EVENT.get(outcome, "worker_error")
        try:
            import notify as notify_mod
            text = notify_mod.build_message(event, issue=issue, pr=pr, tests=tests,
                                            detail=detail)
            return notify_mod.send(text)
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__}

    def attempts(self, issue: Any) -> Dict[str, Any]:
        guard = self.guard()
        task = task_key(issue)
        used = guard.attempts(task)
        return {"issue": issue, "task": task, "attempts": used,
                "retry_cap": guard.max_attempts,
                "retries_left": max(0, guard.max_attempts - used)}

    def report(self, provider: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        guard = self.guard()
        spend = guard.spend_today(provider)
        used = self.state.usage()
        return {
            "budget": {"spend_today_usd": spend["spend"], "basis": spend["basis"],
                       "provider_ok": spend["provider_ok"],
                       "provider_daily_usd": spend["provider_daily"],
                       "daily_cap_usd": guard.daily_usd, "paused": guard.is_paused()},
            "worker": {"issues_today": used["issues"], "cost_today_usd": used["cost"],
                       "issues_cap": lib.MAX_ISSUES_PER_DAY,
                       "cost_cap_usd": lib.MAX_COST_PER_DAY,
                       "cost_per_issue_cap_usd": lib.MAX_COST_PER_ISSUE},
        }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Cổng hợp nhất cho issue worker.")
    parser.add_argument("--state", default=None)
    parser.add_argument("--budget-dir", default=None)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("can-start", help="ngân sách + gate issue cho một issue")
    p.add_argument("--issue", required=True)
    p.add_argument("--estimate", type=float, default=DEFAULT_ESTIMATE)

    p = sub.add_parser("claim", help="giữ chỗ ngân sách rồi claim issue")
    p.add_argument("--issue", required=True)
    p.add_argument("--estimate", type=float, default=DEFAULT_ESTIMATE)
    p.add_argument("--ttl", type=int, default=DEFAULT_TTL)

    p = sub.add_parser("finish", help="nhả chỗ, ghi chi phí, đóng issue, thông báo")
    p.add_argument("--issue", required=True)
    p.add_argument("--outcome", required=True,
                   choices=sorted(OUTCOME_EVENT))
    p.add_argument("--cost", type=float, default=0.0)
    p.add_argument("--pr")
    p.add_argument("--tests")
    p.add_argument("--detail")
    p.add_argument("--note", default="")
    p.add_argument("--no-notify", action="store_true")

    p = sub.add_parser("attempts", help="số lần đã thử và trần retry")
    p.add_argument("--issue", required=True)

    sub.add_parser("report", help="số của cả hai lớp")

    args = parser.parse_args(argv)
    gate = Gate(args.state, args.budget_dir)

    if args.command == "can-start":
        out = gate.can_start(args.issue, args.estimate)
    elif args.command == "claim":
        out = gate.claim(args.issue, args.estimate, args.ttl)
    elif args.command == "finish":
        out = gate.finish(args.issue, args.outcome, args.cost, pr=args.pr,
                          note=args.note, tests=args.tests, notify=not args.no_notify,
                          detail=args.detail)
    elif args.command == "attempts":
        out = gate.attempts(args.issue)
    else:
        out = gate.report()

    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    if args.command in ("can-start", "claim"):
        return 0 if out["allowed"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

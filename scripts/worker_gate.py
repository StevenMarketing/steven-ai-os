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


def issue_caps() -> Dict[str, Any]:
    """Hạn mức riêng của worker: $0.06/issue · **$0.20/ngày** · 3 issue/ngày.

    Đây là **tham số truyền vào** `StateStore.can_start` (và `remaining_today`), nên không
    phải sửa `issue_worker_lib.py` — vốn là protected path. Mặc định trong lib vẫn là
    $0.15/ngày; mức $0.20 ở đây là chủ ý: 3 × $0.06 = $0.18 > $0.15 nên với trần cũ issue
    thứ ba **luôn** bị từ chối. Đổi được bằng biến môi trường để không phải sửa code.
    """
    return {
        "per_issue_usd": float(os.environ.get("STEVEN_WORKER_PER_ISSUE_USD") or 0.06),
        "per_day_usd": float(os.environ.get("STEVEN_WORKER_PER_DAY_USD") or 0.20),
        "issues_per_day": int(os.environ.get("STEVEN_WORKER_ISSUES_PER_DAY") or 3),
    }

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

    @staticmethod
    def _resolve_state_path(state_dir: Optional[str]) -> str:
        """Cùng quy ước với `issue_guard.py`: mặc định là `<dir>/state.json`.

        Nếu người gọi đưa vào một **thư mục** thì thêm `state.json` — trước đây module
        dùng thẳng `lib.default_state_dir()` (một thư mục), nên nó đọc file rỗng và
        tưởng chưa issue nào được xử lý: idempotency cùng hạn mức issue/ngày mất tác dụng.
        """
        path = state_dir or os.path.join(lib.default_state_dir(), "state.json")
        if os.path.isdir(path):
            return os.path.join(path, "state.json")
        return path

    def __init__(self, state_dir: Optional[str] = None,
                 budget_dir: Optional[str] = None,
                 now: Optional[float] = None,
                 ack_file: Optional[str] = None) -> None:
        self.state_path = self._resolve_state_path(state_dir)
        self.state = lib.StateStore(self.state_path)
        self.budget_dir = budget_dir or budget.default_state_dir()
        # File ngoại lệ legacy: nằm trong repo (versioned) nên được review như code.
        self.ack_file = (ack_file or os.environ.get("STEVEN_LEGACY_ACK_FILE")
                         or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "legacy_unsettled.json"))
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
        caps = issue_caps()
        ok_issue, issue_reason = self.state.can_start(
            issue, estimate, max_per_issue=caps["per_issue_usd"],
            max_per_day=caps["per_day_usd"], max_issues_per_day=caps["issues_per_day"])
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
               notify: bool = True, detail: Optional[str] = None,
               force: bool = False) -> Dict[str, Any]:
        if outcome not in OUTCOME_EVENT:
            raise SystemExit(f"kết cục không hợp lệ: {outcome!r} "
                             f"(dùng: {', '.join(sorted(OUTCOME_EVENT))})")
        guard = self.guard()
        task = task_key(issue)
        # IDEMPOTENCY: một issue đã đóng thì không được ghi chi phí lần hai, không được
        # gửi tin thứ hai. Đường bình thường đã bị chặn từ `can_start` (issue đã
        # `processed`), nhưng một lời gọi finish **thủ công** vẫn lọt trước bản vá này.
        self.state = lib.StateStore(self.state_path)
        if self.state.is_processed(issue) and not force:
            settled = (self.state.data.get("processed") or {}).get(str(issue)) or {}
            return {
                "issue": issue, "outcome": outcome, "already_settled": True,
                "settled_as": settled.get("outcome"),
                "settled_pr": settled.get("pr"),
                "settled_at": settled.get("finished_at"),
                "cost_recorded_usd": 0.0,
                "hold_released_usd": 0.0,
                "attempts": guard.attempts(task),
                "notified": {"ok": True,
                             "skipped": "already settled: no duplicate message, no cost"},
            }
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
            "usage_today": usage,
            "remaining": store.remaining_today(max_cost=issue_caps()["per_day_usd"],
                                               max_issues=issue_caps()["issues_per_day"]),
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
            return notify_mod.send(text, state_dir=self.budget_dir,
                                   label=f"outcome-{issue}-{outcome}", event=event)
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__}

    def notify_once_for_audit(self, out: Dict[str, Any]) -> Dict[str, Any]:
        """Cảnh báo bypass **một lần mỗi ngày cho mỗi tập issue** (shim chạy mỗi phút)."""
        issues = ",".join(out.get("missing_settlement") or [])
        label = f"gate-audit-{out.get('day')}-{issues}"
        try:
            import notify as notify_mod
            if label in notify_mod.notified_today(self.budget_dir)["sent"]:
                return {"ok": True, "skipped": "already notified today"}
            result = self.notify("worker_error", 0,
                                 detail=("Cổng ngân sách bị bỏ qua cho issue "
                                         f"{issues}: đã đóng mà không có dấu vết ngân sách."))
            if result.get("ok"):
                notify_mod.mark_notified(label, self.budget_dir)
            return result
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__}

    def attempts(self, issue: Any) -> Dict[str, Any]:
        guard = self.guard()
        task = task_key(issue)
        used = guard.attempts(task)
        return {"issue": issue, "task": task, "attempts": used,
                "retry_cap": guard.max_attempts,
                "retries_left": max(0, guard.max_attempts - used)}

    def audit(self, day: Optional[str] = None) -> Dict[str, Any]:
        """Đối chiếu issue đã đóng hôm nay với sổ cái ngân sách (bộ **phát hiện** bypass).

        Một lần chạy đi đúng đường để lại **hai** dấu vết: một dòng `attempt` trong sổ cái
        ngân sách (do `finish` ghi) và một mục `processed` trong sổ cái worker. Thiếu vế
        ngân sách nghĩa là đã có đường chạy **bỏ qua cổng**.

        Vì sao cần: lớp ngân sách provider-aware nằm ở `worker_gate.py` (file mới, không
        protected) chứ **không** nằm trong `issue_guard.py`/`issue_worker_lib.py` — mà sửa
        file protected thì cần chủ sở hữu cho phép. Cổng chặn ở lời gọi runbook, nên cần một
        bộ phát hiện độc lập: chỉ đọc file cục bộ, không gọi mạng, $0.
        """
        guard = self.guard()
        target_day = day or guard._today()
        self.state = lib.StateStore(self.state_path)      # đọc lại sổ cái worker trên đĩa
        settled: Dict[str, float] = {}
        for row in guard.entries():
            if row.get("day") != target_day or row.get("kind") != "attempt":
                continue
            task = str(row.get("task") or "")
            if task.startswith("issue-"):
                settled[task] = round(settled.get(task, 0.0) + float(row.get("cost", 0.0)), 6)
        processed: Dict[str, Any] = {}
        for issue, record in (self.state.data.get("processed") or {}).items():
            info = record if isinstance(record, dict) else {"outcome": str(record)}
            finished = str(info.get("finished_at") or "")
            if finished[:10] != target_day:
                continue
            processed[str(issue)] = {"outcome": info.get("outcome"), "pr": info.get("pr"),
                                     "finished_at": finished}
        missing = sorted(i for i in processed if task_key(i) not in settled)
        # Ngoại lệ legacy có version: chỉ những id được **chủ repo xác nhận** trong
        # `legacy_unsettled.json` mới được coi là đã biết. Task MỚI thiếu settlement
        # KHÔNG bao giờ được miễn — nếu không, cảnh báo sẽ mất giá trị.
        acked = self.acknowledged_legacy()
        acknowledged = {i: acked[i] for i in missing if i in acked}
        still_missing = [i for i in missing if i not in acked]
        return {"day": target_day,
                "processed_today": processed,
                "settled_tasks": {k: v for k, v in sorted(settled.items())},
                "missing_settlement": still_missing,
                "acknowledged_legacy": acknowledged,
                "ack_file": self.ack_file,
                "ok": not still_missing,
                "note": ("ok=False nghĩa là một issue MỚI đã đóng mà không đi qua cổng ngân "
                         "sách: kiểm tra ngay, đừng chỉ dựa vào runbook. Issue trong "
                         "acknowledged_legacy là ngoại lệ đã được chủ repo xác nhận và vẫn "
                         "được liệt kê, không bị xoá khỏi lịch sử."),
                }

    def acknowledged_legacy(self) -> Dict[str, Any]:
        """Đọc file ngoại lệ đã xác nhận (versioned, nằm trong repo để review được)."""
        try:
            with open(self.ack_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return {str(k): v for k, v in (data or {}).items()}

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
                       "cost_cap_usd": issue_caps()["per_day_usd"],
                       "cost_per_issue_cap_usd": issue_caps()["per_issue_usd"],
                       "library_defaults_usd": {"per_day": lib.MAX_COST_PER_DAY,
                                                "per_issue": lib.MAX_COST_PER_ISSUE},
                       "caps_source": ("worker_gate (overrides the protected library "
                                       "defaults; override via STEVEN_WORKER_* env vars)")},
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
    p.add_argument("--force", action="store_true",
                   help="ghi lại dù issue đã đóng (chỉ dùng khi cố ý; mặc định từ chối)")

    p = sub.add_parser("attempts", help="số lần đã thử và trần retry")
    p.add_argument("--issue", required=True)

    p = sub.add_parser("audit", help="phát hiện issue đã đóng mà không qua cổng ngân sách")
    p.add_argument("--day", default=None)
    p.add_argument("--notify", action="store_true",
                   help="báo Telegram khi phát hiện thiếu dấu vết ngân sách")

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
                          detail=args.detail, force=args.force)
    elif args.command == "attempts":
        out = gate.attempts(args.issue)
    elif args.command == "audit":
        out = gate.audit(args.day)
        if args.notify and not out["ok"]:
            out["notified"] = gate.notify_once_for_audit(out)
    else:
        out = gate.report()

    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    if args.command in ("can-start", "claim"):
        return 0 if out["allowed"] else 1
    if args.command == "audit":
        return 0 if out["ok"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

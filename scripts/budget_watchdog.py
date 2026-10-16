#!/usr/bin/env python3
"""Watchdog ngân sách: dừng worker khi chi tiêu ngày chạm trần.

Thuần script, KHÔNG gọi LLM, KHÔNG in key. Mặc định chỉ **báo cáo**; chỉ hành
động khi bật ``--enforce`` (để chủ sở hữu chủ động quyết định).

Khi ``--enforce`` và chi tiêu ngày (số provider xác nhận, fallback về sổ cái cục
bộ) đã chạm/vượt trần:

* tạo file kill-switch cho worker (``issue_guard pause``),
* tạm dừng cron job của worker (``hermes cron pause <job_id>``) nếu đang chạy,
* thoát mã 3 (dấu hiệu "đã chạm trần") để lớp trên có thể báo động.

Thoát mã: 0 = còn ngân sách · 3 = đã chạm trần · 1 = không đọc được số liệu.

Dùng::

    python3 scripts/budget_watchdog.py                  # chỉ báo cáo
    python3 scripts/budget_watchdog.py --enforce        # dừng worker khi cần
    python3 scripts/budget_watchdog.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import budget  # noqa: E402

DEFAULT_JOB_ID = "e07126dba5a5"


def _hermes(*args: str) -> Dict[str, Any]:
    try:
        proc = subprocess.run(["hermes", *args], capture_output=True, text=True,
                              timeout=60)
        return {"returncode": proc.returncode, "stdout": proc.stdout[-400:]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"returncode": None, "error": str(exc)}


def evaluate(guard: Optional[budget.BudgetGuard] = None,
             provider: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Quyết định watchdog (không tác dụng phụ)."""
    guard = guard or budget.BudgetGuard()
    info = provider if provider is not None else budget.provider_usage()
    local_spend = guard.spent_today()
    provider_today = budget.provider_today(guard.dir, info, guard.now) if info.get("ok") \
        else {"ok": False, "error": info.get("error")}
    # Ưu tiên số provider xác nhận; fallback sổ cái cục bộ (thận trọng hơn).
    if provider_today.get("ok"):
        spend = max(float(provider_today.get("spent_today_usd") or 0.0), local_spend)
        basis = "provider-confirmed (fallback: local ledger)"
    else:
        spend = local_spend
        basis = "local ledger only (provider unreadable)"
    remaining = round(guard.daily_usd - spend, 6)
    over = spend >= guard.daily_usd
    return {
        "daily_cap_usd": guard.daily_usd,
        "spend_today_usd": round(spend, 6),
        "remaining_usd": remaining,
        "basis": basis,
        "over_cap": over,
        "provider": info,
        "provider_today": provider_today,
        "local_ledger_usd": round(local_spend, 6),
        "enforced": guard.is_paused(),
    }


def enforce(guard: budget.BudgetGuard, job_id: str) -> Dict[str, Any]:
    guard.set_paused(True, f"ngân sách ngày đã chạm trần ${guard.daily_usd:.2f}")
    result = {"guard_paused": guard.pause_path, "cron": None}
    listing = _hermes("cron", "list")
    if job_id and job_id in (listing.get("stdout") or ""):
        result["cron"] = _hermes("cron", "pause", job_id)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Watchdog ngân sách Steven AI OS.")
    parser.add_argument("--state-dir", default=budget.default_state_dir())
    parser.add_argument("--job-id", default=os.environ.get("HERMES_WORKER_JOB_ID",
                                                           DEFAULT_JOB_ID))
    parser.add_argument("--enforce", action="store_true",
                        help="thật sự pause worker khi chạm trần")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    guard = budget.BudgetGuard(args.state_dir)
    state = evaluate(guard)
    state["acted"] = False

    if state["over_cap"] and args.enforce:
        state["action"] = enforce(guard, args.job_id)
        state["acted"] = True

    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True, default=str))
    else:
        print(f"Ngân sách ngày ${state['daily_cap_usd']:.2f} · đã dùng "
              f"${state['spend_today_usd']:.4f} · còn ${state['remaining_usd']:.4f} "
              f"({state['basis']})")
        if state["over_cap"]:
            print("ĐÃ CHẠM TRẦN" + (" — đã pause worker" if state["acted"]
                                     else " (chưa enforce; dùng --enforce để dừng worker)"))
        else:
            print("Còn ngân sách — không cần hành động.")

    if not state["provider"].get("ok"):
        return 1
    return 3 if state["over_cap"] else 0


if __name__ == "__main__":
    sys.exit(main())

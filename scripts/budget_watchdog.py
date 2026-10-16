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


def _notify(event: str, detail: str = "", spend: Optional[float] = None,
            cap: Optional[float] = None) -> Dict[str, Any]:
    """Thông báo qua Telegram; lỗi thông báo KHÔNG được làm watchdog thất bại."""
    try:
        import notify
        text = notify.build_message(event, spend=spend, cap=cap, detail=detail)
        return notify.send(text)
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def _notify_once(label: str, event: str, detail: str = "",
                 state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Như `_notify` nhưng mỗi nhãn chỉ gửi tối đa một lần mỗi ngày."""
    try:
        import notify
        if label in notify.notified_today(state_dir)["sent"]:
            return {"ok": True, "skipped": "already notified today"}
        result = _notify(event, detail=detail)
        if result.get("ok"):
            notify.mark_notified(label, state_dir)
        return result
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def evaluate(guard: Optional[budget.BudgetGuard] = None,
             provider: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Quyết định watchdog (không tác dụng phụ).

    Dùng **max(số provider xác nhận, sổ cái cục bộ)** nên chi tiêu của các phiên
    Hermes khác cũng bị tính. Khi provider không đọc được, vẫn phải kết luận được
    \"đã chạm trần\" từ sổ cái — nếu không, watchdog sẽ **im lặng đúng lúc cần
    dừng worker nhất**.
    """
    guard = guard or budget.BudgetGuard()
    info = provider if provider is not None else budget.provider_usage()
    spend_info = guard.spend_today(info)
    spend = float(spend_info["spend"])
    remaining = round(guard.daily_usd - spend, 6)
    over = spend >= guard.daily_usd
    return {
        "daily_cap_usd": guard.daily_usd,
        "spend_today_usd": round(spend, 6),
        "remaining_usd": remaining,
        "basis": spend_info["basis"],
        "over_cap": over,
        "provider": info,
        "provider_ok": spend_info["provider_ok"],
        "provider_daily_usd": spend_info["provider_daily"],
        "local_ledger_usd": round(float(spend_info["ledger"]), 6),
        "unreadable_provider": not spend_info["provider_ok"],
        "counts_local_only": not spend_info["provider_ok"],
        "enforced": guard.is_paused(),
    }


def enforce(guard: budget.BudgetGuard, job_id: str) -> Dict[str, Any]:
    """Pause worker. ``state_changed`` cho biết đây là lần chuyển trạng thái đầu tiên.

    Chỉ thông báo khi trạng thái **đổi**, nếu không thì một watchdog chạy mỗi phút
    sẽ gửi một tin mỗi phút.

    Job của worker có thể nằm ở **profile riêng** (`worker-agent`), nên phải pause ở
    **đúng profile đó** — pause ở profile mặc định sẽ không dừng gì cả. Cả hai nơi đều
    được thử và kết quả được ghi lại để không "im lặng thành công".
    """
    was_paused = guard.is_paused()
    guard.set_paused(True, f"ngân sách ngày đã chạm trần ${guard.daily_usd:.2f}")
    result: Dict[str, Any] = {"guard_paused": guard.pause_path, "cron": None,
                              "cron_default": None, "cron_worker_profile": None,
                              "state_changed": not was_paused}
    profile = os.environ.get("STEVEN_WORKER_PROFILE", "worker-agent")
    listing = _hermes("cron", "list")
    if job_id and job_id in (listing.get("stdout") or ""):
        result["cron"] = _hermes("cron", "pause", job_id)
        result["cron_default"] = result["cron"]
    if job_id and profile:
        worker_listing = _hermes("-p", profile, "cron", "list")
        if job_id in (worker_listing.get("stdout") or ""):
            result["cron_worker_profile"] = _hermes("-p", profile, "cron", "pause", job_id)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Watchdog ngân sách Steven AI OS.")
    parser.add_argument("--state-dir", default=budget.default_state_dir())
    parser.add_argument("--job-id", default=os.environ.get("HERMES_WORKER_JOB_ID",
                                                           DEFAULT_JOB_ID))
    parser.add_argument("--enforce", action="store_true",
                        help="thật sự pause worker khi chạm trần")
    parser.add_argument("--notify", action="store_true",
                        help="gửi thông báo Telegram khi hành động / khi không đọc được số provider")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    guard = budget.BudgetGuard(args.state_dir)
    state = evaluate(guard)
    state["acted"] = False

    if state["over_cap"] and args.enforce:
        state["action"] = enforce(guard, args.job_id)
        state["acted"] = True
        if args.notify and state["action"].get("state_changed", True):
            _notify("budget", detail="Watchdog đã pause worker vì chạm trần ngày.",
                    spend=state["spend_today_usd"], cap=state["daily_cap_usd"])
    elif state["unreadable_provider"] and args.notify:
        # Chỉ báo một lần mỗi ngày: watchdog chạy mỗi phút, nếu không sẽ spam.
        _notify_once("provider-unreadable", "worker_error",
                     detail=("Không đọc được số provider; watchdog chỉ còn sổ cái cục bộ "
                             f"${state['local_ledger_usd']:.4f}."),
                     state_dir=args.state_dir)

    # Mốc 80% / 100% phải báo được **kể cả khi không có issue nào** (worker chỉ chạy khi
    # có việc). notify.py khử trùng theo ngày nên chạy mỗi phút không spam.
    if args.notify:
        try:
            import notify as notify_mod
            thresholds = notify_mod.crossed_thresholds(guard, state_dir=args.state_dir)
            state["thresholds"] = {"spend_usd": thresholds["spend_usd"],
                                   "cap_usd": thresholds["cap_usd"],
                                   "pending": [p["label"] for p in thresholds["pending"]],
                                   "sent": []}
            for item in thresholds["pending"]:
                text = notify_mod.build_message(
                    "budget", spend=thresholds["spend_usd"], cap=thresholds["cap_usd"],
                    detail=f"Mốc {int(item['threshold'] * 100)}% đã vượt.")
                outcome = notify_mod.send(text)
                if outcome.get("ok"):
                    notify_mod.mark_notified(item["label"], args.state_dir)
                    state["thresholds"]["sent"].append(item["label"])
        except Exception as exc:                              # noqa: BLE001
            state["thresholds"] = {"error": type(exc).__name__}

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
        if state["unreadable_provider"]:
            print("CẢNH BÁO: không đọc được số provider — chỉ đếm sổ cái cục bộ.")

    # Thứ tự ưu tiên mã thoát: 3 (đã chạm trần, kể cả khi chỉ đếm sổ cái) > 1 (không
    # đọc được provider) > 0. Trước đây nhánh "không đọc được provider" trả 1 và che
    # mất trạng thái chạm trần ⇒ lớp trên không pause worker.
    if state["over_cap"]:
        return 3
    return 1 if state["unreadable_provider"] else 0


if __name__ == "__main__":
    sys.exit(main())

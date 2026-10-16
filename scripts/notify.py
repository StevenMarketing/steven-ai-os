"""Thông báo Telegram cho Steven AI OS — script thuần, không gọi LLM.

Nguyên tắc:
* Dùng lại cấu hình Telegram **đã có** (``hermes send`` đọc ``.env`` +
  ``config.yaml`` của gateway) ⇒ không tạo bot/token/connector mới.
* KHÔNG in token, chat id hay credential vào tin nhắn.
* KHÔNG gửi khi polling không có việc: chỉ gửi theo sự kiện được yêu cầu.
* Mốc ngân sách 80% / 100% chỉ gửi **một lần mỗi ngày** cho mỗi mốc (trạng thái
  lưu tại ``ops/budget/notify-state.json``) để không spam.

Sự kiện: ``task_done`` · ``task_failed`` · ``task_blocked`` · ``budget`` ·
``worker_error`` · ``custom``.

Dùng::

    python3 scripts/notify.py --event task_done --issue 2 --pr URL --tests "101 passed"
    python3 scripts/notify.py --event task_blocked --issue 2 --detail "approval denied"
    python3 scripts/notify.py --event budget --check-thresholds
    python3 scripts/notify.py --text "..." --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import budget  # noqa: E402

THRESHOLDS = (0.80, 1.00)
Runner = Callable[[Sequence[str]], Dict[str, Any]]


def _hermes_cmd(*args: str) -> list:
    """Lệnh `hermes send` chạy qua profile chỉ định (mặc định: "default").

    Worker chạy trong profile `worker-agent`, nhưng profile đó không có session Telegram
    nào để mirror thông báo vào. Profile mặc định thì có kênh đã hoạt động, nên thông báo
    được gửi qua đó; khoá inference của worker KHÔNG bị ảnh hưởng — đây chỉ là đường gửi.
    Đặt ``STEVEN_NOTIFY_PROFILE=current`` để giữ hành vi cũ (gửi trong profile hiện tại).
    """
    profile = (os.environ.get("STEVEN_NOTIFY_PROFILE") or "default").strip()
    if not profile or profile == "current":
        return ["hermes", "send", *args]
    return ["hermes", "-p", profile, "send", *args]

def default_runner(args: Sequence[str]) -> Dict[str, Any]:
    """Gửi qua Hermes (đọc credential sẵn có). Không truyền --json để tránh in chat id."""
    try:
        proc = subprocess.run(_hermes_cmd(*args), capture_output=True,
                              text=True, timeout=60)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": (proc.stdout or "").strip()[-300:]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": type(exc).__name__}


def build_message(event: str, **fields: Any) -> str:
    """Soạn tin nhắn. Chỉ dùng các trường cho phép — không có chỗ cho bí mật."""
    issue = fields.get("issue")
    pr = fields.get("pr")
    tests = fields.get("tests")
    detail = fields.get("detail")
    spend = fields.get("spend")
    cap = fields.get("cap")
    head = "Steven AI OS"
    if event == "task_done":
        lines = [f"{head} — task xong, đã có draft PR."]
        if issue:
            lines.append(f"Issue: #{issue}")
        if pr:
            lines.append(f"PR: {pr}")
        if tests:
            lines.append(f"Tests: {tests}")
        lines.append("Cần người review/merge — worker không tự merge.")
        return "\n".join(lines)
    if event == "task_failed":
        lines = [f"{head} — task THẤT BẠI."]
        if issue:
            lines.append(f"Issue: #{issue}")
        if detail:
            lines.append(f"Chi tiết: {detail}")
        return "\n".join(lines)
    if event == "task_blocked":
        lines = [f"{head} — task BLOCKED, cần người xử lý."]
        if issue:
            lines.append(f"Issue: #{issue}")
        if detail:
            lines.append(f"Lý do: {detail}")
        return "\n".join(lines)
    if event == "budget":
        pct = ""
        if spend is not None and cap:
            try:
                pct = f" ({float(spend) / float(cap) * 100:.0f}%)"
            except (TypeError, ValueError, ZeroDivisionError):
                pct = ""
        lines = [f"{head} — cảnh báo ngân sách{pct}."]
        if spend is not None and cap:
            lines.append(f"Đã dùng ${float(spend):.4f} / trần ${float(cap):.2f}.")
        if detail:
            lines.append(detail)
        return "\n".join(lines)
    if event == "worker_error":
        lines = [f"{head} — lỗi worker/cron cần chú ý."]
        if detail:
            lines.append(f"Chi tiết: {detail}")
        return "\n".join(lines)
    return str(fields.get("text") or f"{head} — thông báo.")


def redact_safe(text: str) -> str:
    """Chặn mọi mảnh giống credential lọt vào tin nhắn."""
    for marker in ("sk-or-", "github_pat_", "ghp_", "Bearer ", "OPENROUTER_API_KEY",
                   "TELEGRAM_BOT_TOKEN", "term_unused"):
        if marker in text:
            return "Steven AI OS — cảnh báo: nội dung thông báo chứa mẫu giống credential "
            "nên đã bị chặn gửi. Kiểm tra log cục bộ."
    return text


def delivery_log_path(state_dir: Optional[str] = None) -> str:
    """Đường dẫn sổ gửi. `STEVEN_NOTIFY_LEDGER_FILE` cho phép test cô lập hoàn toàn."""
    override = os.environ.get("STEVEN_NOTIFY_LEDGER_FILE")
    if override:
        return override
    return os.path.join(state_dir or budget.default_state_dir(), "notify-delivery.jsonl")


def record_delivery(text: str, ok: bool, returncode: Optional[int] = None,
                    state_dir: Optional[str] = None, label: Optional[str] = None,
                    event: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Ghi **kết quả gửi** (không ghi nội dung): phát hiện lỗi gửi và gửi trùng.

    Sổ này là nguồn sự thật để trả lời "tin đã đi chưa / đi mấy lần", thứ mà trước đây
    không có. Chỉ lưu: thời điểm, nhãn, băm nội dung, độ dài, mã trả về — **không** lưu
    nội dung tin nên không thể lộ bí mật qua sổ này.
    """
    import hashlib
    stamp = time.gmtime(now if now is not None else time.time())
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", stamp),
        "day": time.strftime("%Y-%m-%d", stamp),
        "label": label, "event": event,
        "ok": bool(ok), "returncode": returncode,
        "sha": hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12],
        "chars": len(text or ""),
    }
    path = delivery_log_path(state_dir)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as exc:
        row["log_error"] = type(exc).__name__
    return row


def audit_deliveries(state_dir: Optional[str] = None,
                     day: Optional[str] = None) -> Dict[str, Any]:
    """Đọc sổ gửi: lần gửi lỗi và nội dung bị gửi lặp trong cùng ngày."""
    day = day or time.strftime("%Y-%m-%d", time.gmtime())
    rows: list = []
    try:
        with open(delivery_log_path(state_dir), "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    today = [r for r in rows if r.get("day") == day]
    failures = [r for r in today if not r.get("ok")]
    seen: Dict[str, int] = {}
    for row in today:
        if row.get("ok"):
            seen[row.get("sha")] = seen.get(row.get("sha"), 0) + 1
    duplicates = {sha: count for sha, count in seen.items() if count > 1}
    return {"day": day, "sent": len(today), "failures": failures,
            "duplicate_hashes": duplicates,
            "ok": not failures and not duplicates,
            "note": ("failures = tin KHÔNG gửi được; duplicate_hashes = cùng nội dung gửi "
                     "nhiều lần trong ngày.")}


def send(text: str, *, target: str = "telegram", dry_run: bool = False,
         runner: Optional[Runner] = None, state_dir: Optional[str] = None,
         label: Optional[str] = None, event: Optional[str] = None) -> Dict[str, Any]:
    safe = redact_safe(text)
    if dry_run:
        return {"ok": True, "dry_run": True, "message": safe}
    runner = runner or default_runner
    result = runner(["-t", target, safe])
    result["message"] = safe
    result["delivery"] = record_delivery(safe, bool(result.get("ok")),
                                        result.get("returncode"), state_dir, label, event)
    return result


def state_path(state_dir: Optional[str] = None) -> str:
    return os.path.join(state_dir or budget.default_state_dir(), "notify-state.json")


def notified_today(state_dir: Optional[str] = None,
                   now: Optional[float] = None) -> Dict[str, Any]:
    day = time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))
    try:
        with open(state_path(state_dir), "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        record = {}
    if record.get("day") != day:
        return {"day": day, "sent": []}
    return {"day": day, "sent": list(record.get("sent") or [])}


def mark_notified(label: str, state_dir: Optional[str] = None,
                  now: Optional[float] = None) -> None:
    record = notified_today(state_dir, now)
    if label not in record["sent"]:
        record["sent"].append(label)
    path = state_path(state_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)


def crossed_thresholds(guard: budget.BudgetGuard,
                       provider: Optional[Dict[str, Any]] = None,
                       state_dir: Optional[str] = None,
                       now: Optional[float] = None) -> Dict[str, Any]:
    """Mốc đã vượt nhưng chưa thông báo hôm nay."""
    info = provider if provider is not None else budget.provider_usage()
    spend_info = guard.spend_today(info)
    spend = float(spend_info["spend"])
    cap = float(guard.daily_usd)
    already = set(notified_today(state_dir or guard.dir, now)["sent"])
    pending = []
    if cap > 0:
        for threshold in THRESHOLDS:
            label = f"budget-{int(threshold * 100)}"
            if spend >= cap * threshold and label not in already:
                pending.append({"label": label, "threshold": threshold})
    return {"spend_usd": spend, "cap_usd": cap, "basis": spend_info["basis"],
            "already_notified": sorted(already), "pending": pending,
            "provider_ok": spend_info["provider_ok"]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Thông báo Telegram cho Steven AI OS.")
    parser.add_argument("--event", default="custom",
                        choices=["task_done", "task_failed", "task_blocked", "budget",
                                 "worker_error", "custom"])
    parser.add_argument("--issue")
    parser.add_argument("--pr")
    parser.add_argument("--tests")
    parser.add_argument("--detail")
    parser.add_argument("--text")
    parser.add_argument("--target", default="telegram")
    parser.add_argument("--state-dir", default=budget.default_state_dir())
    parser.add_argument("--check-thresholds", action="store_true",
                        help="chỉ gửi nếu vượt mốc 80%%/100%% chưa thông báo")
    parser.add_argument("--audit-deliveries", action="store_true",
                        help="đọc sổ gửi: lần gửi lỗi và nội dung gửi lặp trong ngày")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.audit_deliveries:
        report = audit_deliveries(args.state_dir)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
        else:
            print(f"ngày {report['day']}: đã gửi {report['sent']} · lỗi "
                  f"{len(report['failures'])} · nội dung trùng {report['duplicate_hashes']}")
        return 0 if report["ok"] else 1

    if args.event == "budget" and args.check_thresholds:
        guard = budget.BudgetGuard(args.state_dir)
        result = crossed_thresholds(guard, state_dir=args.state_dir)
        sent = []
        for item in result["pending"]:
            text = build_message("budget", spend=result["spend_usd"],
                                 cap=result["cap_usd"],
                                 detail=f"Mốc {int(item['threshold'] * 100)}% đã vượt.")
            outcome = send(text, target=args.target, dry_run=args.dry_run)
            sent.append({"label": item["label"], "ok": outcome.get("ok")})
            if outcome.get("ok") and not args.dry_run:
                mark_notified(item["label"], args.state_dir)
        payload = {"checked": True, "spend_usd": result["spend_usd"],
                   "cap_usd": result["cap_usd"], "basis": result["basis"],
                   "sent": sent, "pending": [p["label"] for p in result["pending"]]}
        print(json.dumps(payload, indent=2) if args.json else
              f"ngân sách ${result['spend_usd']:.4f}/${result['cap_usd']:.2f} "
              f"· đã gửi: {[s['label'] for s in sent] or 'không'}")
        return 0

    text = args.text or build_message(args.event, issue=args.issue, pr=args.pr,
                                      tests=args.tests, detail=args.detail)
    if args.event == "budget":
        guard = budget.BudgetGuard(args.state_dir)
        spend_info = guard.spend_today()
        text = build_message("budget", spend=spend_info["spend"], cap=guard.daily_usd,
                             detail=args.detail)
    outcome = send(text, target=args.target, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(outcome, indent=2))
    else:
        print(("DRY-RUN: " if args.dry_run else "sent: ") + outcome.get("message", ""))
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

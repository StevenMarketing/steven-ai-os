"""Steven AI OS — ngân sách và định tuyến model.

Mục tiêu: biết mình đã tiêu bao nhiêu, theo từng task / model / ngày; chặn khi
chạm trần; và đề xuất model theo độ phức tạp — mà KHÔNG tự đổi model đang chạy,
KHÔNG tự tăng ngân sách, KHÔNG bao giờ in hoặc ghi API key.

Hai nguồn số liệu, luôn tách bạch:

* **Ước tính (Hermes)** — bảng ``sessions`` trong ``state.db``:
  ``estimated_cost_usd`` + token/API call, theo session.
* **Provider xác nhận (OpenRouter)** — ``GET https://openrouter.ai/api/v1/key``:
  ``usage`` / ``limit`` / còn lại. Đây là con số dùng cho hoá đơn.

Cơ chế cưỡng chế trung thực: Hermes **không** có hard cap ở cấp provider. Trần
cứng duy nhất là hạn mức khoá OpenRouter (``limit``); trần **theo ngày** do module
này cưỡng chế bằng code, và nó **fail-closed**: không đọc được provider ⇒ từ chối
chi tiêu (trừ khi bật ``STEVEN_BUDGET_FAIL_OPEN=1``).

CLI::

    python3 scripts/budget.py report [--days 1] [--json]
    python3 scripts/budget.py status
    python3 scripts/budget.py can-spend --estimate 0.05 --task issue-42
    python3 scripts/budget.py record --task issue-42 --cost 0.0086 --model deepseek-v4.1-flash
    python3 scripts/budget.py route --task-class docs
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Trần ngân sách (đọc từ môi trường; code KHÔNG bao giờ tự tăng)
# --------------------------------------------------------------------------
DEFAULT_DAILY_USD = 3.00          # ngân sách chủ sở hữu phê duyệt
DEFAULT_TASK_USD = 0.60           # trần cho một task đơn lẻ
DEFAULT_MAX_ATTEMPTS = 3          # chống vòng lặp đốt token
OPENROUTER_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_HOST = "openrouter.ai"
HTTP_TIMEOUT = 20

#: transport(url, token) -> dict (dùng để test không cần mạng)
Transport = Callable[[str, str], Dict[str, Any]]

# --------------------------------------------------------------------------
# Định tuyến model: chỉ ĐỀ XUẤT, không tự đổi model đang chạy
# --------------------------------------------------------------------------
MODEL_TIERS: Dict[str, Dict[str, Any]] = {
    "economy": {
        "model": "deepseek/deepseek-v4.1-flash",
        "provider": "openrouter",
        "use_for": ("docs", "polling", "triage", "small-fix", "test-add",
                    "issue-worker", "status-report"),
        "note": "model đang chạy; đủ cho mọi việc thường ngày",
    },
    "strong": {
        "model": "(chưa cấu hình — chủ sở hữu chọn khi cần)",
        "provider": "openrouter",
        "use_for": ("architecture", "security-review", "large-refactor",
                    "migration-design"),
        "note": "chỉ dùng khi độ phức tạp thực sự cần; phải được chủ sở hữu duyệt",
    },
}

#: loại task -> hạng model
TASK_CLASS_TIER: Dict[str, str] = {
    "docs": "economy",
    "polling": "economy",
    "triage": "economy",
    "small-fix": "economy",
    "test-add": "economy",
    "issue-worker": "economy",
    "status-report": "economy",
    "architecture": "strong",
    "security-review": "strong",
    "large-refactor": "strong",
    "migration-design": "strong",
}


def route(task_class: str) -> Dict[str, Any]:
    """Hạng model đề xuất cho một loại task (không thay đổi cấu hình)."""
    tier = TASK_CLASS_TIER.get(task_class, "economy")
    chosen = dict(MODEL_TIERS[tier])
    chosen["tier"] = tier
    chosen["task_class"] = task_class
    return chosen


# --------------------------------------------------------------------------
# Nguồn 1: ước tính của Hermes (state.db, chỉ đọc)
# --------------------------------------------------------------------------
def state_db_path() -> str:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(home, "state.db")


def _task_class_of(row: Dict[str, Any]) -> str:
    source = str(row.get("source") or "unknown")
    if source == "cron":
        session_id = str(row.get("id") or "")
        match = re.match(r"cron_([0-9a-f]+)_", session_id)
        return "github-issue-worker" if match else "cron"
    return source


def collect_estimates(db_path: Optional[str] = None,
                      days: int = 1,
                      now: Optional[float] = None) -> Dict[str, Any]:
    """Tổng hợp ước tính chi phí của Hermes theo ngày / model / task."""
    path = db_path or state_db_path()
    cutoff = (now if now is not None else time.time()) - days * 86400
    out: Dict[str, Any] = {
        "source": "hermes-estimated", "db": path, "window_days": days,
        "sessions": [], "by_day": {}, "by_model": {}, "by_task": {},
        "totals": {"sessions": 0, "api_calls": 0, "messages": 0,
                   "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                   "estimated_cost_usd": 0.0},
    }
    if not os.path.isfile(path):
        out["note"] = "state.db không tồn tại"
        return out
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, source, model, started_at, ended_at, message_count,"
            " tool_call_count, api_call_count, input_tokens, output_tokens,"
            " cache_read_tokens, reasoning_tokens, estimated_cost_usd,"
            " actual_cost_usd, cost_status, cost_source"
            " FROM sessions WHERE started_at >= ? ORDER BY started_at",
            (cutoff,)).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        out["note"] = f"không đọc được state.db: {exc}"
        return out

    def bucket(key: str) -> Dict[str, Any]:
        return out["by_day"].setdefault(
            key, {"sessions": 0, "api_calls": 0, "estimated_cost_usd": 0.0})

    for row in rows:
        data = dict(row)
        day = time.strftime("%Y-%m-%d", time.gmtime(float(data["started_at"] or 0)))
        cost = float(data.get("estimated_cost_usd") or 0.0)
        task = _task_class_of(data)
        model = str(data.get("model") or "unknown")
        out["sessions"].append({
            "id": data["id"], "day": day, "task": task, "model": model,
            "messages": data.get("message_count"), "api_calls": data.get("api_call_count"),
            "input_tokens": data.get("input_tokens"),
            "output_tokens": data.get("output_tokens"),
            "cache_read_tokens": data.get("cache_read_tokens"),
            "estimated_cost_usd": round(cost, 6),
            "actual_cost_usd": data.get("actual_cost_usd"),
            "cost_status": data.get("cost_status"),
        })
        totals = out["totals"]
        totals["sessions"] += 1
        totals["api_calls"] += int(data.get("api_call_count") or 0)
        totals["messages"] += int(data.get("message_count") or 0)
        totals["input_tokens"] += int(data.get("input_tokens") or 0)
        totals["output_tokens"] += int(data.get("output_tokens") or 0)
        totals["cache_read_tokens"] += int(data.get("cache_read_tokens") or 0)
        totals["estimated_cost_usd"] = round(totals["estimated_cost_usd"] + cost, 6)

        bucket(day)
        out["by_day"][day]["sessions"] += 1
        out["by_day"][day]["api_calls"] += int(data.get("api_call_count") or 0)
        out["by_day"][day]["estimated_cost_usd"] = round(
            out["by_day"][day]["estimated_cost_usd"] + cost, 6)

        model_row = out["by_model"].setdefault(
            model, {"sessions": 0, "api_calls": 0, "estimated_cost_usd": 0.0})
        model_row["sessions"] += 1
        model_row["api_calls"] += int(data.get("api_call_count") or 0)
        model_row["estimated_cost_usd"] = round(model_row["estimated_cost_usd"] + cost, 6)

        task_row = out["by_task"].setdefault(
            task, {"sessions": 0, "api_calls": 0, "estimated_cost_usd": 0.0})
        task_row["sessions"] += 1
        task_row["api_calls"] += int(data.get("api_call_count") or 0)
        task_row["estimated_cost_usd"] = round(task_row["estimated_cost_usd"] + cost, 6)

    out["sessions"].sort(key=lambda s: s["estimated_cost_usd"], reverse=True)
    return out


# --------------------------------------------------------------------------
# Nguồn 2: provider xác nhận (OpenRouter)
# --------------------------------------------------------------------------
def http_get_json(url: str, token: str) -> Dict[str, Any]:
    if urllib.parse.urlparse(url).hostname != OPENROUTER_HOST:
        raise ValueError(f"từ chối gọi host khác: {url}")
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def read_env_value(name: str, env_file: Optional[str] = None) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = env_file or os.path.join(
        os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"), ".env")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, _, raw = line.strip().partition("=")
                if key.strip() == name:
                    return raw.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _sanitize_budgets(raw: Any) -> List[Dict[str, Any]]:
    """Chỉ giữ các trường ngân sách cần thiết (bỏ id nội bộ của provider)."""
    out: List[Dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        out.append({key: item.get(key) for key in
                    ("scope", "limit_usd", "spend_usd", "remaining_usd",
                     "reset_interval", "resets_at")})
    return out


def provider_usage(token: Optional[str] = None, transport: Optional[Transport] = None,
                   env_file: Optional[str] = None) -> Dict[str, Any]:
    """Số provider xác nhận. Không bao giờ trả về hay in token."""
    if transport is None:
        secret = token if token is not None else read_env_value("OPENROUTER_API_KEY", env_file)
        if not secret:
            return {"ok": False, "error": "không tìm thấy OPENROUTER_API_KEY"}
        transport = http_get_json
        payload = transport(OPENROUTER_URL, secret)
    else:
        payload = transport(OPENROUTER_URL, token or "test-key")
    data = (payload or {}).get("data") or {}
    limit = data.get("limit")
    usage = data.get("usage") or 0
    try:
        remaining = None if limit is None else round(float(limit) - float(usage), 6)
    except (TypeError, ValueError):
        remaining = None

    def _num(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return round(float(value), 6)
        except (TypeError, ValueError):
            return None

    return {
        "ok": True,
        "label": None,                    # cố ý bỏ: nhãn khoá là thông tin nhạy cảm
        "usage_usd": round(float(usage), 6) if usage else 0.0,
        "limit_usd": None if limit is None else float(limit),
        "remaining_usd": (remaining if remaining is not None
                          else _num(data.get("limit_remaining"))),
        "usage_daily_usd": _num(data.get("usage_daily")),
        "usage_weekly_usd": _num(data.get("usage_weekly")),
        "usage_monthly_usd": _num(data.get("usage_monthly")),
        "budgets": _sanitize_budgets(data.get("budgets")),
        "expires_at": data.get("expires_at"),
        "is_free_tier": data.get("is_free_tier"),
        "source": "provider-confirmed",
    }


def provider_today(state_dir: Optional[str] = None, usage: Optional[Dict[str, Any]] = None,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """Chênh lệch usage của provider trong ngày (mốc đầu ngày lưu tại chỗ)."""
    snapshot = os.path.join(state_dir or default_state_dir(), "provider-usage.json")
    day = time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))
    record: Dict[str, Any] = {}
    try:
        with open(snapshot, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        record = {}
    current = usage or provider_usage()
    if not current.get("ok"):
        return {"ok": False, "error": current.get("error", "provider không khả dụng"),
                "day": day}
    if record.get("day") != day or "baseline" not in record:
        # Ngày mới: nối tiếp mốc cuối của ngày trước để không "quên" phần đã tiêu
        # (nếu chỉ lấy giá trị hiện tại làm mốc thì mọi khoản trước đó biến mất).
        record = {"day": day,
                  "baseline": float(record.get("last", current.get("usage_usd", 0.0)))}
    record["last"] = current.get("usage_usd", 0.0)
    record["limit_usd"] = current.get("limit_usd")
    record["remaining_usd"] = current.get("remaining_usd")
    try:
        os.makedirs(os.path.dirname(snapshot) or ".", exist_ok=True)
        with open(snapshot, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
    except OSError:
        pass
    spent = round(float(record["last"]) - float(record["baseline"]), 6)
    daily = current.get("usage_daily_usd")
    # Ưu tiên số theo ngày do provider xác nhận; chỉ dùng mốc cục bộ khi provider
    # không cung cấp trường đó.
    if daily is None:
        spent_today, basis = max(0.0, spent), "local baseline delta"
    else:
        spent_today, basis = float(daily), "provider usage_daily"
    return {"ok": True, "day": day, "spent_today_usd": spent_today,
            "basis": basis,
            "total_usage_usd": record["last"], "limit_usd": record.get("limit_usd"),
            "remaining_usd": record.get("remaining_usd"),
            "usage_weekly_usd": current.get("usage_weekly_usd"),
            "usage_monthly_usd": current.get("usage_monthly_usd")}


# --------------------------------------------------------------------------
# Trần chi tiêu + sổ cái
# --------------------------------------------------------------------------
def default_state_dir() -> str:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(home, "ops", "budget")


class BudgetGuard:
    """Cưỡng chế trần ngân sách bằng code (fail-closed)."""

    def __init__(self, state_dir: Optional[str] = None,
                 daily_usd: Optional[float] = None,
                 task_usd: Optional[float] = None,
                 max_attempts: Optional[int] = None,
                 now: Optional[float] = None) -> None:
        self.dir = state_dir or default_state_dir()
        self.daily_usd = float(daily_usd if daily_usd is not None
                               else os.environ.get("STEVEN_DAILY_BUDGET_USD", DEFAULT_DAILY_USD))
        self.task_usd = float(task_usd if task_usd is not None
                              else os.environ.get("STEVEN_TASK_BUDGET_USD", DEFAULT_TASK_USD))
        self.max_attempts = int(max_attempts if max_attempts is not None
                                else os.environ.get("STEVEN_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
        self.now = now

    # -- io ----------------------------------------------------------------
    @property
    def ledger_path(self) -> str:
        return os.path.join(self.dir, "ledger.jsonl")

    @property
    def pause_path(self) -> str:
        return os.path.join(self.dir, "PAUSED")

    def is_paused(self) -> bool:
        return os.path.exists(self.pause_path)

    def entries(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return rows

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.now if self.now is not None
                                                     else time.time()))

    def spent_today(self) -> float:
        day = self._today()
        return round(sum(float(r.get("cost", 0.0)) for r in self.entries()
                         if r.get("day") == day), 6)

    def attempts(self, task: str) -> int:
        return sum(1 for r in self.entries() if str(r.get("task")) == str(task))

    # -- ghi ---------------------------------------------------------------
    def record(self, task: str, cost: float, model: str = "", note: str = "",
               provider_confirmed_usd: Optional[float] = None) -> Dict[str, Any]:
        os.makedirs(self.dir, exist_ok=True)
        row = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now if self.now is not None
                                                                  else time.time())),
            "day": self._today(), "task": str(task), "cost": round(float(cost), 6),
            "model": model, "note": note,
            "provider_confirmed_usd": (None if provider_confirmed_usd is None
                                       else round(float(provider_confirmed_usd), 6)),
        }
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".ledger-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for existing in self.entries():
                    handle.write(json.dumps(existing, sort_keys=True) + "\n")
                handle.write(json.dumps(row, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.ledger_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return {"recorded": row, "spent_today_usd": self.spent_today(),
                "attempts": self.attempts(task)}

    # -- cưỡng chế ---------------------------------------------------------
    def can_spend(self, estimate: float, task: Optional[str] = None,
                  provider: Optional[Dict[str, Any]] = None,
                  fail_open: Optional[bool] = None) -> Tuple[bool, str]:
        if self.is_paused():
            return False, f"ngân sách đang PAUSED ({self.pause_path})"
        estimate = float(estimate)
        if estimate > self.task_usd:
            return False, (f"ước tính ${estimate:.4f} vượt trần một task "
                           f"${self.task_usd:.2f}")
        if task is not None and self.attempts(task) >= self.max_attempts:
            return False, (f"task {task} đã thử {self.attempts(task)} lần "
                           f"(trần retry {self.max_attempts})")
        spent = self.spent_today()
        if spent + estimate > self.daily_usd:
            return False, (f"trần ngày: đã tiêu ${spent:.4f} + ${estimate:.4f} > "
                           f"${self.daily_usd:.2f}")
        info = provider if provider is not None else provider_usage()
        if info.get("ok"):
            remaining = info.get("remaining_usd")
            if remaining is not None and remaining <= 0:
                return False, "provider báo hết hạn mức (remaining <= 0)"
            if remaining is not None and remaining < estimate:
                return False, (f"hạn mức provider còn ${remaining:.4f} < ước tính "
                               f"${estimate:.4f}")
        else:
            allow = os.environ.get("STEVEN_BUDGET_FAIL_OPEN", "0") == "1" if fail_open is None \
                else bool(fail_open)
            if not allow:
                return False, (f"không đọc được số provider ({info.get('error')}) "
                               "- fail-closed")
        return True, "ok"

    def status(self, provider: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        info = provider if provider is not None else provider_usage()
        return {
            "limits": {"daily_usd": self.daily_usd, "task_usd": self.task_usd,
                       "max_attempts": self.max_attempts},
            "spent_today_usd": self.spent_today(),
            "remaining_today_usd": round(max(0.0, self.daily_usd - self.spent_today()), 6),
            "paused": self.is_paused(),
            "provider": info,
            "provider_today": provider_today(self.dir, info, self.now) if info.get("ok")
            else {"ok": False, "error": info.get("error")},
            "entries_today": len([r for r in self.entries() if r.get("day") == self._today()]),
            "note": ("Trần theo ngày do module này cưỡng chế bằng code; Hermes KHÔNG có "
                     "hard cap ở cấp provider. Trần cứng duy nhất là hạn mức khoá "
                     "OpenRouter (limit_usd)."),
        }

    def set_paused(self, paused: bool, reason: str = "") -> None:
        os.makedirs(self.dir, exist_ok=True)
        if paused:
            with open(self.pause_path, "w", encoding="utf-8") as handle:
                handle.write((reason or "paused") + "\n")
        elif os.path.exists(self.pause_path):
            os.unlink(self.pause_path)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _print_report(report: Dict[str, Any]) -> None:
    totals = report["totals"]
    print(f"Ước tính của Hermes ({report['window_days']} ngày) — {report['db']}")
    print(f"  sessions={totals['sessions']} api_calls={totals['api_calls']} "
          f"in={totals['input_tokens']} out={totals['output_tokens']}")
    print(f"  ƯỚC TÍNH: ${totals['estimated_cost_usd']:.6f}")
    print("  Theo ngày:")
    for day, row in sorted(report["by_day"].items()):
        print(f"    {day}  ${row['estimated_cost_usd']:.6f}  "
              f"({row['sessions']} session, {row['api_calls']} api)")
    print("  Theo model:")
    for model, row in sorted(report["by_model"].items(),
                             key=lambda kv: -kv[1]["estimated_cost_usd"]):
        print(f"    {model:34} ${row['estimated_cost_usd']:.6f}  ({row['sessions']} session)")
    print("  Theo task:")
    for task, row in sorted(report["by_task"].items(),
                            key=lambda kv: -kv[1]["estimated_cost_usd"]):
        print(f"    {task:34} ${row['estimated_cost_usd']:.6f}  ({row['sessions']} session)")
    print("  Session tốn nhất:")
    for row in report["sessions"][:5]:
        print(f"    {row['id'][:46]:46} {row['task']:14} ${row['estimated_cost_usd']:.6f}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ngân sách Steven AI OS.")
    parser.add_argument("--state-dir", default=default_state_dir())
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="chi phí ước tính theo ngày/model/task")
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", help="trần ngân sách + số provider xác nhận")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("can-spend", help="có được chi tiêu không?")
    p.add_argument("--estimate", type=float, required=True)
    p.add_argument("--task")

    p = sub.add_parser("record", help="ghi một khoản chi vào sổ cái")
    p.add_argument("--task", required=True)
    p.add_argument("--cost", type=float, required=True)
    p.add_argument("--model", default="")
    p.add_argument("--note", default="")

    p = sub.add_parser("route", help="hạng model đề xuất cho một loại task")
    p.add_argument("--task-class", required=True)

    p = sub.add_parser("pause", help="dừng chi tiêu (kill switch)")
    p.add_argument("--reason", default="")

    sub.add_parser("resume", help="bật lại")

    args = parser.parse_args(argv)
    guard = BudgetGuard(args.state_dir)

    if args.command == "report":
        report = collect_estimates(days=args.days)
        print(json.dumps(report, indent=2) if args.json else "", end="")
        if not args.json:
            _print_report(report)
        return 0
    if args.command == "status":
        info = guard.status()
        print(json.dumps(info, indent=2, sort_keys=True) if args.json else "")
        if not args.json:
            limits = info["limits"]
            print(f"Trần ngày ${limits['daily_usd']:.2f} · trần task ${limits['task_usd']:.2f} "
                  f"· retry {limits['max_attempts']}")
            print(f"Đã ghi hôm nay: ${info['spent_today_usd']:.6f} "
                  f"(còn ${info['remaining_today_usd']:.4f}) · paused={info['paused']}")
            prov = info.get("provider") or {}
            if prov.get("ok"):
                print(f"Provider: dùng ${prov['usage_usd']:.4f}/"
                      f"{prov['limit_usd']} · còn ${prov['remaining_usd']}")
            else:
                print(f"Provider: KHÔNG đọc được ({prov.get('error')}) => fail-closed")
        return 0
    if args.command == "can-spend":
        allowed, reason = guard.can_spend(args.estimate, args.task)
        print(json.dumps({"allowed": allowed, "reason": reason,
                          "estimate": args.estimate, "task": args.task}, indent=2))
        return 0 if allowed else 1
    if args.command == "record":
        print(json.dumps(guard.record(args.task, args.cost, args.model, args.note),
                         indent=2))
        return 0
    if args.command == "route":
        print(json.dumps(route(args.task_class), indent=2))
        return 0
    if args.command in ("pause", "resume"):
        guard.set_paused(args.command == "pause", getattr(args, "reason", ""))
        print(f"ngân sách {'PAUSED' if args.command == 'pause' else 'RUNNABLE'}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

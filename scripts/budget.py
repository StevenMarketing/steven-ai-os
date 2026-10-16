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
DEFAULT_DAILY_USD = 4.00          # trần toàn hệ thống do chủ sở hữu phê duyệt:
#                                   chat $3/ngày + worker $1/ngày (đều reset daily ở provider)
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


KEY_ROLES = (("worker", "WORKER_OPENROUTER_API_KEY"), ("chat", "OPENROUTER_API_KEY"))
KEY_NAMES = tuple(name for _role, name in KEY_ROLES)


def _aggregate_keys(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Cộng số của nhiều khoá thành trần hệ thống; giữ breakdown theo vai trò.

    Trần $3/ngày là của **toàn hệ thống**, nên khi worker có khoá riêng thì con số phải
    là **tổng** hai khoá, không phải chỉ khoá này hay khoá kia. Breakdown chỉ gồm vai trò +
    số liệu; **không** chứa tên khoá, nhãn khoá hay giá trị khoá.
    """
    ok_parts = [p for p in parts if p.get("ok")]

    def _sum(field: str) -> Optional[float]:
        values = [p.get(field) for p in ok_parts if p.get(field) is not None]
        return round(sum(values), 6) if values else None

    budgets: List[Dict[str, Any]] = []
    for part in ok_parts:
        for budget in part.get("budgets") or []:
            budgets.append(dict(budget, role=part.get("role")))
    return {
        "ok": bool(ok_parts) or all(not p.get("ok") for p in parts) and bool(parts),
        "label": None,
        "keys_read": [p.get("role") for p in parts],
        "usage_usd": _sum("usage_usd") or 0.0,
        "limit_usd": _sum("limit_usd"),
        "remaining_usd": _sum("remaining_usd"),
        "usage_daily_usd": _sum("usage_daily_usd"),
        "usage_weekly_usd": _sum("usage_weekly_usd"),
        "usage_monthly_usd": _sum("usage_monthly_usd"),
        "budgets": budgets,
        "expires_at": min([p.get("expires_at") for p in ok_parts if p.get("expires_at")] or [None]),
        "is_free_tier": all(p.get("is_free_tier") for p in ok_parts) if ok_parts else None,
        "source": "provider-confirmed (all keys)",
        "per_key": [{"role": p.get("role"), "ok": bool(p.get("ok")),
                     "usage_daily_usd": p.get("usage_daily_usd"),
                     "limit_usd": p.get("limit_usd"),
                     "remaining_usd": p.get("remaining_usd"),
                     "error": p.get("error")} for p in parts],
        "errors": [{"role": p.get("role"), "error": p.get("error")} for p in parts
                   if not p.get("ok")],
    }


def worker_key_usage(token: Optional[str] = None,
                     transport: Optional[Transport] = None) -> Dict[str, Any]:
    """Số của **riêng khoá worker** (nếu có). Dùng để biết hạn mức cứng của worker.

    Khoá worker được tìm theo thứ tự: biến `WORKER_OPENROUTER_API_KEY`, rồi tới
    `OPENROUTER_API_KEY` trong file `.env` của profile worker
    (`STEVEN_WORKER_ENV_FILE`, mặc định `/opt/data/profiles/worker-agent/.env`).
    """
    default_worker_env = "/opt/data/profiles/worker-agent/.env"
    worker_file = os.environ.get("STEVEN_WORKER_ENV_FILE", default_worker_env)
    secret = token or read_env_value("WORKER_OPENROUTER_API_KEY")
    if not secret and worker_file:
        secret = read_env_value("OPENROUTER_API_KEY", worker_file)
    if not secret:
        return {"ok": False, "role": "worker", "configured": False,
                "worker_env_file": worker_file,
                "error": ("chưa cấu hình khoá worker: đặt OPENROUTER_API_KEY trong "
                          f"{worker_file} (hoặc WORKER_OPENROUTER_API_KEY)")}
    try:
        payload = (transport or http_get_json)(OPENROUTER_URL, secret)
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "role": "worker", "configured": True,
                "worker_env_file": worker_file, "error": type(exc).__name__}
    parsed = _parse_provider_payload(payload, source="provider-confirmed (worker key)")
    return {**parsed, "role": "worker", "configured": True, "worker_env_file": worker_file}


def provider_usage(token: Optional[str] = None, transport: Optional[Transport] = None,
                   env_file: Optional[str] = None,
                   provider_file: Optional[str] = None) -> Dict[str, Any]:
    """Số provider xác nhận. Không bao giờ trả về hay in token.

    Khi không có `token`/`transport`/`provider_file` cụ thể, hàm đọc **mọi khoá** đã cấu
    hình (`WORKER_OPENROUTER_API_KEY` trước, rồi `OPENROUTER_API_KEY`) và **cộng lại**, vì
    trần $3/ngày là của toàn hệ thống. Giá trị khoá không bao giờ xuất hiện trong kết quả.

    ``provider_file`` (hoặc biến môi trường ``STEVEN_BUDGET_PROVIDER_FILE``) đọc
    phản hồi provider từ file JSON: dùng cho test offline và môi trường không có
    mạng — nhờ đó test không phụ thuộc số thật của tài khoản.
    """
    source_path = provider_file or os.environ.get("STEVEN_BUDGET_PROVIDER_FILE")
    if source_path:
        try:
            with open(source_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            return {"ok": False,
                    "error": f"không đọc được provider-file ({type(exc).__name__})"}
        return _parse_provider_payload(payload, source="provider-file")
    if token is None and transport is None and env_file is None:
        parts: List[Dict[str, Any]] = []
        worker_file = (os.environ.get("STEVEN_WORKER_ENV_FILE")
                       or "/opt/data/profiles/worker-agent/.env")
        for role, name in KEY_ROLES:
            if role == "worker":
                # Khoá worker có thể nằm ở biến riêng, hoặc trong .env của profile worker
                # (nơi nó được đặt tên OPENROUTER_API_KEY cho inference). Chỉ đọc, không in.
                secret = read_env_value("WORKER_OPENROUTER_API_KEY")
                if not secret and worker_file:
                    secret = read_env_value("OPENROUTER_API_KEY", worker_file)
            else:
                secret = read_env_value(name)
            if not secret:
                continue
            try:
                parsed = _parse_provider_payload(http_get_json(OPENROUTER_URL, secret),
                                                 source="provider-confirmed")
            except Exception as exc:                          # noqa: BLE001
                parsed = {"ok": False, "error": type(exc).__name__}
            parts.append(dict(parsed, role=role))
        if parts:
            return _aggregate_keys(parts)
        return {"ok": False,
                "error": (f"không tìm thấy khoá nào trong {', '.join(KEY_NAMES)}"
                          + (f" (đã thử đọc {os.path.basename(worker_file)} cho vai trò worker)"
                             if worker_file else
                             " (chưa đặt STEVEN_WORKER_ENV_FILE cho khoá worker)"))}
    if transport is None:
        secret = token if token is not None else read_env_value("OPENROUTER_API_KEY", env_file)
        if not secret:
            return {"ok": False, "error": "không tìm thấy OPENROUTER_API_KEY"}
        transport = http_get_json
        payload = transport(OPENROUTER_URL, secret)
    else:
        payload = transport(OPENROUTER_URL, token or "test-key")
    return _parse_provider_payload(payload, source="provider-confirmed")


def _parse_provider_payload(payload: Any, source: str = "provider-confirmed") -> Dict[str, Any]:
    data = (payload or {}).get("data") or {}
    limit = data.get("limit")
    usage = data.get("usage") or 0

    def _num(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return round(float(value), 6)
        except (TypeError, ValueError):
            return None

    # `usage` là TỔNG luỹ kế, còn `limit` có thể **reset theo ngày** ⇒ `limit - usage` cho
    # số vô nghĩa (thậm chí âm) khi hạn mức theo kỳ, và gate sẽ từ chối mọi việc vì tưởng
    # hết hạn mức. Vì vậy ưu tiên `limit_remaining` do provider trả về.
    reported_remaining = _num(data.get("limit_remaining"))
    try:
        computed_remaining = _num(float(limit) - float(usage)) if limit is not None else None
    except (TypeError, ValueError):
        computed_remaining = None
    remaining = reported_remaining if reported_remaining is not None else computed_remaining
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
        "source": source,
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


class BudgetLock:
    """Khoá liên tiến trình cho vùng tới hạn của ngân sách (chống TOCTOU)."""

    def __init__(self, path: str, timeout: float = 10.0, poll: float = 0.05) -> None:
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        try:
            import fcntl
        except ImportError:                                   # pragma: no cover
            return True
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return True
            except OSError:
                if time.time() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(self.poll)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except ImportError:                                   # pragma: no cover
            pass
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "BudgetLock":
        if not self.acquire():
            raise TimeoutError(f"không lấy được khoá ngân sách {self.path}")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


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
        """Chỉ sổ cái cục bộ (không gồm chi tiêu của các phiên Hermes khác)."""
        day = self._today()
        return round(sum(float(r.get("cost", 0.0)) for r in self.entries()
                         if r.get("day") == day), 6)

    def spend_today(self, provider: Optional[Dict[str, Any]] = None,
                    fail_open: Optional[bool] = None) -> Dict[str, Any]:
        """Chi tiêu trong ngày = **max(số provider, sổ cái cục bộ)**.

        Lý do dùng ``max`` chứ không cộng: sổ cái cục bộ là **tập con** của số
        provider (mọi khoản sổ cái ghi đều nằm trong ``usage_daily``). Cộng hai
        nguồn sẽ **đếm trùng**.

        Khi provider không trả ``usage_daily``, dùng mốc nối tiếp theo ngày; khi
        không đọc được provider thì chỉ còn sổ cái và đánh dấu ``ok=False`` để
        người gọi fail-closed.
        """
        info = provider if provider is not None else provider_usage()
        ledger = self.spent_today()
        provider_daily: Optional[float] = None
        if info.get("ok"):
            daily = info.get("usage_daily_usd")
            if daily is not None:
                provider_daily = float(daily)
            else:
                snapshot = provider_today(self.dir, info, self.now)
                if snapshot.get("ok"):
                    provider_daily = float(snapshot["spent_today_usd"])
        if provider_daily is None:
            return {"ok": bool(info.get("ok")), "spend": ledger, "basis": "local ledger",
                    "provider_daily": None, "ledger": ledger,
                    "provider_ok": bool(info.get("ok")),
                    "provider_error": info.get("error")}
        return {"ok": True, "spend": max(provider_daily, ledger),
                "basis": "max(provider usage_daily, local ledger)",
                "provider_daily": provider_daily, "ledger": ledger,
                "provider_ok": True, "provider_error": None}

    def attempts(self, task: str) -> int:
        kind = "attempt"
        return sum(1 for r in self.entries()
                   if str(r.get("task")) == str(task)
                   and r.get("kind", kind) == kind)

    # -- ghi ---------------------------------------------------------------
    def record(self, task: str, cost: float, model: str = "", note: str = "",
               provider_confirmed_usd: Optional[float] = None,
               kind: str = "attempt") -> Dict[str, Any]:
        os.makedirs(self.dir, exist_ok=True)
        row = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now if self.now is not None
                                                                  else time.time())),
            "day": self._today(), "task": str(task), "cost": round(float(cost), 6),
            "model": model, "note": note, "kind": kind,
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
    @property
    def lock_path(self) -> str:
        return os.path.join(self.dir, "budget.lock")

    def _fail_open_flag(self, fail_open: Optional[bool]) -> bool:
        if fail_open is not None:
            return bool(fail_open)
        return os.environ.get("STEVEN_BUDGET_FAIL_OPEN", "0") == "1"

    def can_spend(self, estimate: float, task: Optional[str] = None,
                  provider: Optional[Dict[str, Any]] = None,
                  fail_open: Optional[bool] = None) -> Tuple[bool, str]:
        """Kiểm tra thuần (không khoá, không ghi) — dùng được cho câu hỏi nhanh.

        Số theo ngày lấy từ **max(provider usage_daily, sổ cái)** nên chi tiêu của
        các phiên Hermes khác cũng được tính, và không bị đếm trùng.
        """
        if self.is_paused():
            return False, f"ngân sách đang PAUSED ({self.pause_path})"
        estimate = float(estimate)
        if estimate > self.task_usd:
            return False, (f"ước tính ${estimate:.4f} vượt trần một task "
                           f"${self.task_usd:.2f}")
        if task is not None and self.attempts(task) >= self.max_attempts:
            return False, (f"task {task} đã thử {self.attempts(task)} lần "
                           f"(trần retry {self.max_attempts})")
        info = provider if provider is not None else provider_usage()
        spend = self.spend_today(info)
        allow = self._fail_open_flag(fail_open)
        if not spend["ok"]:
            if not allow:
                return False, (f"không đọc được số provider "
                               f"({spend.get('provider_error')}) - fail-closed")
        elif spend["spend"] + estimate > self.daily_usd:
            return False, (f"trần ngày: đã tiêu ${spend['spend']:.4f} "
                           f"[{spend['basis']}] + ${estimate:.4f} > "
                           f"${self.daily_usd:.2f}")
        if info.get("ok"):
            remaining = info.get("remaining_usd")
            if remaining is not None and remaining <= 0:
                return False, "provider báo hết hạn mức (remaining <= 0)"
            if remaining is not None and remaining < estimate:
                return False, (f"hạn mức provider còn ${remaining:.4f} < ước tính "
                               f"${estimate:.4f}")
        elif not allow:
            return False, (f"không đọc được số provider ({info.get('error')}) "
                           "- fail-closed")
        return True, "ok"

    def reserve(self, task: str, estimate: float,
                provider: Optional[Dict[str, Any]] = None,
                fail_open: Optional[bool] = None,
                timeout: float = 10.0) -> Tuple[bool, str]:
        """Giữ chỗ ngân sách **nguyên tử** cho một task (chống race condition).

        Hai task chạy song song không thể cùng vượt trần: phần kiểm tra và phần
        ghi chỗ được thực hiện trong cùng một khoá liên tiến trình.
        """
        with BudgetLock(self.lock_path, timeout=timeout):
            allowed, reason = self.can_spend(estimate, task, provider, fail_open)
            if not allowed:
                return False, reason
            self.record(task, estimate, note="reservation", kind="reserve")
            return True, "reserved"

    def release(self, task: str) -> float:
        """Bỏ các chỗ đã giữ của một task; trả về tổng đã bỏ."""
        freed = 0.0
        for row in self.entries():
            if str(row.get("task")) == str(task) and row.get("kind") == "reserve":
                if row.get("day") == self._today():
                    freed += float(row.get("cost", 0.0))
        if freed:
            os.makedirs(self.dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".ledger-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    for row in self.entries():
                        if (str(row.get("task")) == str(task)
                                and row.get("kind") == "reserve"
                                and row.get("day") == self._today()):
                            continue
                        handle.write(json.dumps(row, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.ledger_path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return round(freed, 6)

    def status(self, provider: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        info = provider if provider is not None else provider_usage()
        spend = self.spend_today(info)
        return {
            "limits": {"daily_usd": self.daily_usd, "task_usd": self.task_usd,
                       "max_attempts": self.max_attempts},
            "spent_today_usd": spend["spend"],
            "spend_basis": spend["basis"],
            "ledger_today_usd": spend["ledger"],
            "provider_daily_usd": spend["provider_daily"],
            "remaining_today_usd": round(max(0.0, self.daily_usd - spend["spend"]), 6),
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
    p.add_argument("--reserve", action="store_true",
                   help="giữ chỗ nguyên tử (chống hai task cùng vượt trần)")

    p = sub.add_parser("release", help="bỏ chỗ ngân sách đã giữ của một task")
    p.add_argument("--task", required=True)

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
        if args.reserve:
            allowed, reason = guard.reserve(args.task or "ad-hoc", args.estimate)
        else:
            allowed, reason = guard.can_spend(args.estimate, args.task)
        print(json.dumps({"allowed": allowed, "reason": reason,
                          "estimate": args.estimate, "task": args.task,
                          "reserved": bool(args.reserve and allowed)}, indent=2))
        return 0 if allowed else 1
    if args.command == "release":
        print(json.dumps({"task": args.task, "freed_usd": guard.release(args.task)},
                         indent=2))
        return 0
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

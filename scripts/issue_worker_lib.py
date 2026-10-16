"""Pure, offline logic for the Steven AI OS GitHub issue worker.

Everything the worker must decide -- is the issue authorised, is the body a
usable task, is the approval genuine, is a path off-limits to automation, does
the text try to hijack the agent, has this already been processed, is there
budget left -- lives here as pure functions plus a small state store, so it can
be unit-tested with fixtures and **no network and no credentials**.

Nothing in this module calls GitHub, reads a token, or writes inside the repo.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

try:  # POSIX file locking; the worker is Linux-only by design
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

# --------------------------------------------------------------------------
# Labels / vocabulary
# --------------------------------------------------------------------------
LABEL_PROPOSED = "hermes-proposed"
LABEL_READY = "hermes-ready"
LABEL_RUNNING = "hermes-running"
LABEL_NEEDS_INFO = "hermes-needs-info"
LABEL_BLOCKED = "hermes-blocked"
LABEL_DONE = "hermes-done"

#: only an approval recorded by one of these logins activates the worker
DEFAULT_OWNER = "StevenMarketing"

#: every one of these H2 sections must be present and non-trivial
REQUIRED_SECTIONS = (
    "goal",
    "scope",
    "acceptance criteria",
    "tests required",
    "constraints",
)
#: a section must carry at least this much content to count as filled in
MIN_SECTION_CHARS = 8
MAX_BODY_CHARS = 16_000

#: words that mean "I have not written this yet"
PLACEHOLDER_WORDS = ("todo", "tbd", "tba", "n/a", "na", "none", "?", "...", "…", "-")

_CHECKBOX = re.compile(r"^[\-*+]\s*\[[ xX]\]\s*")
_NUMBERING = re.compile(r"^\d+[.)]\s*")
_BULLET = re.compile(r"^[\-*+•]\s*")

# --------------------------------------------------------------------------
# Budget (self-enforced -- Hermes has no provider-level USD cap)
# --------------------------------------------------------------------------
MAX_COST_PER_ISSUE = 0.06
MAX_COST_PER_DAY = 0.15
MAX_ISSUES_PER_DAY = 3
DEFAULT_ISSUE_ESTIMATE = 0.05
CLAIM_TTL_SECONDS = 1800

# --------------------------------------------------------------------------
# Paths automation may never touch (must be done by a human)
# --------------------------------------------------------------------------
PROTECTED_PATTERNS: Tuple[str, ...] = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/secrets*",
    "*secret*.json",
    "secrets.json",
    "secrets.yml",
    "secrets.yaml",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    ".gitignore",
    ".github/workflows/**",
    ".github/ISSUE_TEMPLATE/**",
    ".github/rulesets/**",
    "src/gbp/auth/**",
    "src/gbp/authz/**",
    "src/gbp/security/**",
    "sql/schema.sql",
    "scripts/issue_poller.py",
    "scripts/issue_guard.py",
    "scripts/issue_worker_lib.py",
    "scripts/issue_github.py",
    "scripts/issue_pr.py",
    "scripts/hooks/**",
    "scripts/cron/**",
    "tests/test_issue_worker.py",
    "tests/test_issue_worker_hardening.py",
    "tests/fixtures/github_issues.json",
    "docs/GITHUB-WORKER.md",
)

# --------------------------------------------------------------------------
# Prompt-injection tripwires (issue text is untrusted DATA, never instructions)
# --------------------------------------------------------------------------
INJECTION_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("override_instructions",
     r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+"
     r"(?:instructions?|prompts?|rules?|context)"),
    ("disregard_rules", r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)"),
    ("role_switch", r"\b(?:you are now|from now on you|act as (?:a|an)\s+\w+ assistant)\b"),
    ("new_instructions", r"\bnew instructions?\s*:"),
    ("system_prompt", r"\bsystem prompt\b"),
    ("credential_request",
     r"\b(?:reveal|print|show|dump|exfiltrate|send|paste|leak)\b[^\n]{0,30}"
     r"\b(?:token|api[\s_-]?key|secret|credential|password|\.env)\b"),
    ("read_env", r"\b(?:read|open|cat|print|echo)\b[^\n]{0,25}\.env\b"),
    ("env_var_dump", r"\b(?:env|printenv|export)\b\s*\|"),
    ("push_main", r"\b(?:push|commit|merge)\b[^\n]{0,40}\b(?:to\s+|into\s+|onto\s+)?main\b"),
    ("force_push", r"force[\s_-]?push"),
    ("disable_tests", r"\b(?:skip|disable|remove|delete|ignore)\b[^\n]{0,25}\btests?\b"),
    ("network_fetch", r"\b(?:curl|wget)\b[^\n]{0,60}(?:\||>|http)"),
    ("decode_blob", r"\bbase64\s+(?:-d|--decode)\b"),
    ("token_export", r"\bexport\s+GITHUB_TOKEN\b"),
    ("add_credential", r"\badd\b[^\n]{0,25}\b(?:new\s+)?(?:secret|token|credential|api[\s_-]?key)\b"),
    ("weaken_guard",
     r"\b(?:change|edit|remove|disable|bypass|relax)\b[^\n]{0,30}"
     r"\b(?:allowlist|denylist|protected|guard|ruleset|hook|worker)\b"),
    ("self_merge", r"\b(?:merge|approve)\s+(?:the\s+|this\s+)?(?:pr|pull request)\b"),
)

# --------------------------------------------------------------------------
# Secret shapes (high confidence -- no known false positive in this project)
# --------------------------------------------------------------------------
SECRET_SHAPES: Tuple[Tuple[str, str], ...] = (
    ("openrouter_key", r"\bsk-or-v1-[A-Za-z0-9]{25,}\b"),
    ("openai_key", r"\bsk-[A-Za-z0-9]{32,}\b"),
    ("google_api_key", r"\bAIza[0-9A-Za-z\-_]{30,}\b"),
    ("github_token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    ("github_pat", r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ("aws_access_key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("slack_token", r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
    ("private_key_block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("telegram_bot_token", r"\b\d{9,10}:[A-Za-z0-9_\-]{30,}\b"),
)

#: files that legitimately carry fake credential shapes (tests)
TEST_PATH_HINTS = ("test_", "tests/", "fixture", "example", "sample", "docs/")

AUDIT_FILE = "audit.jsonl"


# --------------------------------------------------------------------------
# Issue body handling
# --------------------------------------------------------------------------
def split_sections(body: Optional[str]) -> Dict[str, str]:
    """Split a markdown issue body into ``{lowercased heading: content}``.

    Any heading level (``#``/``##``/``###``) counts, so the template's exact
    heading depth is not load-bearing.
    """
    sections: Dict[str, str] = {}
    current: Optional[str] = None
    buffer: List[str] = []
    for line in (body or "").splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = match.group(1).strip().lower()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def section_content(value: Optional[str]) -> str:
    """Section text with checkbox / bullet / numbering markers and TODO words gone.

    ``- [ ] the widget renders a ZIP column`` -> ``the widget renders a ZIP column``
    ``- [ ]``                                 -> ```` (nothing)
    """
    meaningful: List[str] = []
    for line in (value or "").splitlines():
        text = line.strip()
        for pattern in (_CHECKBOX, _NUMBERING, _BULLET):
            text = pattern.sub("", text).strip()
        if not text:
            continue
        if text.lower().rstrip(":.!") in PLACEHOLDER_WORDS:
            continue
        meaningful.append(text)
    return " ".join(meaningful)


def section_is_placeholder(value: Optional[str]) -> bool:
    """True only when a section carries NO real content at all.

    A genuine acceptance-criteria checklist -- ``- [ ] the widget renders a ZIP
    column`` -- is real content even while every box is still unticked, which is
    exactly how the issue template asks for it. An empty checkbox, or a bare
    TODO/TBD/``-``, is not.
    """
    return not section_content(value).strip()


def validate_task(body: Optional[str]) -> List[str]:
    """Return a list of problems; empty means the body is a usable task."""
    problems: List[str] = []
    text = body or ""
    if not text.strip():
        return ["body is empty"]
    if len(text) > MAX_BODY_CHARS:
        problems.append(
            f"body too large ({len(text)} chars > {MAX_BODY_CHARS}) -- split the task"
        )
    sections = split_sections(text)
    for name in REQUIRED_SECTIONS:
        if name not in sections:
            problems.append(f"missing section: {name.title()}")
            continue
        value = sections[name]
        content = section_content(value)
        if not content:
            # only checkboxes with no text, a bare TODO, or nothing at all
            problems.append(f"section not filled in: {name.title()}")
        elif len(content) < MIN_SECTION_CHARS:
            problems.append(f"section too thin: {name.title()}")
    return problems


def scan_injection(text: Optional[str]) -> List[str]:
    """Return the names of every injection tripwire the text trips."""
    hits: List[str] = []
    haystack = text or ""
    for name, pattern in INJECTION_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            hits.append(name)
    return hits


def label_names(issue: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for item in issue.get("labels") or []:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = item
        if name:
            out.append(str(name))
    return out


def timeline_events(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Events oldest-first. Sorted by ``created_at`` when every event has one."""
    items = [event for event in (events or []) if isinstance(event, dict)]
    if items and all(event.get("created_at") for event in items):
        return sorted(items, key=lambda event: str(event.get("created_at")))
    return items


def label_state(events: Iterable[Dict[str, Any]],
                label: str = LABEL_READY) -> Dict[str, Any]:
    """Who currently holds ``label``, replaying the timeline properly.

    The label must be *live*: an approval that was later removed and re-applied
    by somebody else does not carry the original owner's authority. Historical
    label events that are no longer in effect are therefore ignored, and a
    missing timeline yields no approval at all (fail-closed).
    """
    present = False
    by: Optional[str] = None
    for event in timeline_events(events):
        if ((event.get("label") or {}).get("name")) != label:
            continue
        kind = str(event.get("event") or "")
        if kind == "labeled":
            present = True
            by = (event.get("actor") or {}).get("login")
            by = str(by) if by else None
        elif kind == "unlabeled":
            present = False
            by = None
    return {"present": present, "by": by}


def approval_actor(events: Iterable[Dict[str, Any]],
                   label: str = LABEL_READY) -> Optional[str]:
    """Login that currently holds ``label``, or None."""
    return label_state(events, label)["by"]


def is_authorised(issue: Dict[str, Any], events: Iterable[Dict[str, Any]],
                  owners: Sequence[str] = (DEFAULT_OWNER,)) -> Tuple[bool, str]:
    """The gate: ``hermes-ready`` must be *live* and applied by an owner.

    ``hermes-proposed`` alone never activates anything -- an agent or bot may
    create and label a proposal, but only a human owner's approval counts. A
    label whose timeline was removed, or whose timeline is unavailable, is
    refused rather than trusted.
    """
    labels = label_names(issue)
    if LABEL_READY not in labels:
        return False, f"missing label {LABEL_READY}"
    state = label_state(events)
    if not state["present"]:
        return False, f"no live {LABEL_READY} approval in the issue timeline"
    if state["by"] is None:
        return False, f"{LABEL_READY} approval has no recorded actor"
    if state["by"] not in tuple(owners):
        return False, f"{LABEL_READY} was applied by {state['by']!r}, not an owner"
    return True, f"approved by {state['by']}"


def eligibility(issue: Dict[str, Any], events: Iterable[Dict[str, Any]],
                owners: Sequence[str] = (DEFAULT_OWNER,)) -> Dict[str, Any]:
    """Full verdict for one issue (shape mirrors what the poller reports)."""
    authorised, reason = is_authorised(issue, events, owners)
    problems = validate_task(issue.get("body"))
    injections = scan_injection(issue.get("body"))
    eligible = authorised and not problems and not injections
    if not eligible:
        # The reason must name the ACTUAL cause: an approval that is missing, a
        # task that cannot be used, or a tripwire -- never "approved by X" for an
        # issue that is being refused for another reason.
        if not authorised:
            pass                                   # keep the approval reason
        elif injections:
            reason = "refused: prompt-injection tripwire " + ",".join(injections)
        else:
            reason = "refused: task not usable -- " + "; ".join(problems)
    return {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "eligible": eligible,
        "authorised": authorised,
        "reason": reason,
        "problems": problems,
        "injection": injections,
        "updated_at": issue.get("updated_at"),
    }


def change_fingerprint(issues: Iterable[Dict[str, Any]]) -> str:
    """Deterministic hash of the eligible set -- the cron monitor gate.

    Must contain no timestamps of its own, otherwise every tick would look
    changed and the agent would run (and cost money) on every poll.
    """
    parts = []
    for issue in issues or []:
        parts.append("|".join((
            str(issue.get("number")),
            str(issue.get("updated_at") or ""),
            str(issue.get("title") or ""),
        )))
    digest = hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()
    return digest[:32]


# --------------------------------------------------------------------------
# Branch + path policy
# --------------------------------------------------------------------------
def branch_for(issue_number: Any, title: Optional[str] = None,
               prefix: str = "hermes/issue") -> str:
    """``hermes/issue-<n>-<slug>``; the slug is aggressively sanitised."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    slug = slug[:40].strip("-")
    base = f"{prefix}-{int(issue_number)}"
    return f"{base}-{slug}" if slug else base


def normalise_path(path: str) -> str:
    clean = str(path).replace("\\", "/").strip()
    while clean.startswith("./"):
        clean = clean[2:]
    return clean.lstrip("/")


def path_decision(path: str) -> Tuple[bool, str]:
    """(allowed, reason) for one repo-relative path."""
    clean = normalise_path(path)
    if not clean:
        return False, "empty path"
    if clean.startswith(".git/") or clean == ".git":
        return False, "git internals are off-limits"
    for pattern in PROTECTED_PATTERNS:
        if fnmatch.fnmatch(clean, pattern) or fnmatch.fnmatch(clean, f"**/{pattern}"):
            return False, f"protected path ({pattern}) -- needs a human"
    return True, "ok"


def review_paths(paths: Iterable[str]) -> Dict[str, List[Dict[str, str]]]:
    allowed: List[Dict[str, str]] = []
    blocked: List[Dict[str, str]] = []
    for path in paths or []:
        ok, reason = path_decision(path)
        (allowed if ok else blocked).append({"path": normalise_path(path),
                                             "reason": reason})
    return {"allowed": allowed, "blocked": blocked}


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------
def scan_secrets(text: Optional[str], path: str = "") -> List[Dict[str, str]]:
    """Credential-shaped findings, suppressing the project's test fixtures."""
    findings: List[Dict[str, str]] = []
    haystack = text or ""
    in_test_context = any(hint in normalise_path(path) for hint in TEST_PATH_HINTS)
    for label, pattern in SECRET_SHAPES:
        for match in re.finditer(pattern, haystack):
            findings.append({
                "label": label,
                "path": normalise_path(path),
                "masked": _mask(match.group(0)),
                "context": "test/fixture" if in_test_context else "REVIEW",
            })
    return findings


def scan_against_live_values(text: str, live: Dict[str, str],
                             path: str = "") -> List[Dict[str, str]]:
    """Exact-substring check against real credentials read from the environment.

    Catches a leak that matches no pattern at all. Values are never returned.
    """
    findings: List[Dict[str, str]] = []
    for name, value in (live or {}).items():
        if value and len(value) >= 12 and value in text:
            findings.append({"label": f"live_credential:{name}",
                             "path": normalise_path(path),
                             "masked": "<redacted>", "context": "BLOCK"})
    return findings


def _mask(value: str) -> str:
    value = value or ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}…{value[-3:]}({len(value)})"


# --------------------------------------------------------------------------
# State store (idempotency, claims, budget counters)
# --------------------------------------------------------------------------
class ClaimError(RuntimeError):
    """Raised when an issue is already claimed by a live worker."""


class LockTimeout(RuntimeError):
    """Another process held the worker lock for longer than the timeout."""


class InterProcessLock:
    """Exclusive advisory lock shared by every worker process.

    Uses ``fcntl.flock`` on a lock file, so two overlapping cron ticks (or a
    cron tick plus a manual run) cannot enter the same critical section. The
    kernel drops the lock when the holder dies, so a crashed worker can never
    wedge it permanently.
    """

    def __init__(self, path: str, timeout: float = 10.0, poll: float = 0.05) -> None:
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.time() + max(0.0, float(self.timeout))
        while True:
            try:
                if fcntl is not None:
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
        fd, self._fd = self._fd, None
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "InterProcessLock":
        if not self.acquire():
            raise LockTimeout(f"another worker holds {self.path}")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def default_state_dir() -> str:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(home, "ops", "github-worker")


def today(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class StateStore:
    """Small JSON-backed ledger: processed issues, claims, daily usage."""

    VERSION = 1

    def __init__(self, path: str, now: Optional[float] = None) -> None:
        self.path = path
        self.dir = os.path.dirname(path) or "."
        self.data = self._load()
        self._now = now

    # -- io ---------------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        data.setdefault("version", self.VERSION)
        data.setdefault("processed", {})
        data.setdefault("claims", {})
        data.setdefault("usage", {})
        return data

    def save(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _clock(self) -> float:
        return self._now if self._now is not None else time.time()

    # -- pause switch ------------------------------------------------------
    @property
    def pause_path(self) -> str:
        return os.path.join(self.dir, "PAUSED")

    def is_paused(self) -> bool:
        return os.path.exists(self.pause_path)

    def set_paused(self, paused: bool, reason: str = "") -> None:
        os.makedirs(self.dir, exist_ok=True)
        if paused:
            with open(self.pause_path, "w", encoding="utf-8") as fh:
                fh.write((reason or "paused") + "\n")
        elif os.path.exists(self.pause_path):
            os.unlink(self.pause_path)

    # -- processed / idempotency ------------------------------------------
    def is_processed(self, issue: Any) -> bool:
        return str(issue) in self.data["processed"]

    def mark_processed(self, issue: Any, outcome: str, pr: Optional[str] = None,
                       cost: float = 0.0, note: str = "") -> None:
        self.data["processed"][str(issue)] = {
            "outcome": outcome,
            "pr": pr,
            "cost": round(float(cost), 6),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())),
            "note": note,
        }

    # -- inter-process locking ---------------------------------------------
    @property
    def lock_path(self) -> str:
        return os.path.join(self.dir, "worker.lock")

    @property
    def claims_dir(self) -> str:
        return os.path.join(self.dir, "claims")

    def claim_path(self, issue: Any) -> str:
        return os.path.join(self.claims_dir, f"issue-{issue}.lock")

    @contextmanager
    def transaction(self, timeout: float = 10.0) -> Iterator["StateStore"]:
        """Exclusive, reload-then-mutate-then-save critical section."""
        with InterProcessLock(self.lock_path, timeout=timeout):
            self.data = self._load()
            yield self
            self.save()

    def worker_lock(self, timeout: float = 0.0) -> InterProcessLock:
        """Global single-worker lock (for wrapping a whole worker run)."""
        lock = InterProcessLock(self.lock_path, timeout=timeout)
        return lock

    # -- claims ------------------------------------------------------------
    def claim(self, issue: Any, ttl: int = CLAIM_TTL_SECONDS,
              run_id: Optional[str] = None, timeout: float = 10.0) -> str:
        """Atomic, expiring, cross-process claim.

        Three independent guards, so no two workers can hold the same issue even
        when two cron ticks overlap:

        1. an ``O_EXCL`` marker file -- atomic in the kernel, no lock needed;
        2. the flock'd ledger, reloaded inside the critical section;
        3. the TTL, so a crashed worker's claim is reclaimable.
        """
        key = str(issue)
        run = run_id or hashlib.sha256(f"{key}:{self._clock()}".encode()).hexdigest()[:12]
        expires_at = self._clock() + max(1, int(ttl))
        with InterProcessLock(self.lock_path, timeout=timeout):
            self.data = self._load()
            marker = self.claim_path(key)
            ledger = self.data["claims"].get(key)
            if (ledger and float(ledger.get("expires_at", 0)) > self._clock()) \
                    or self.marker_is_fresh(marker):
                held_by = (ledger or self.read_marker(marker) or {}).get("run_id")
                raise ClaimError(
                    f"issue {key} is already claimed by run {held_by} "
                    f"(expires at {int(expires_at - max(1, int(ttl)) + ttl)})")
            if os.path.exists(marker):          # stale marker: take it over
                os.unlink(marker)
            os.makedirs(self.claims_dir, exist_ok=True)
            payload = json.dumps({"run_id": run, "expires_at": expires_at,
                                  "claimed_at": time.strftime(
                                      "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))})
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError as exc:      # lost the atomic race
                raise ClaimError(
                    f"issue {key} was claimed by another process at the same instant"
                ) from exc
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            self.data["claims"][key] = {"run_id": run,
                                        "claimed_at": json.loads(payload)["claimed_at"],
                                        "expires_at": expires_at}
            self.save()
        return run

    def release(self, issue: Any, timeout: float = 10.0) -> None:
        key = str(issue)
        with InterProcessLock(self.lock_path, timeout=timeout):
            self.data = self._load()
            self._release_unlocked(key)
            self.save()

    def _release_unlocked(self, key: str) -> None:
        """Drop a claim while the caller already holds the worker lock.

        Never called directly by a process that has not taken ``lock_path``:
        ``flock`` is per open file description, so a nested acquisition in the
        same process would block until the timeout.
        """
        self.data["claims"].pop(str(key), None)
        marker = self.claim_path(str(key))
        if os.path.exists(marker):
            os.unlink(marker)

    def read_marker(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def marker_is_fresh(self, path: str) -> bool:
        marker = self.read_marker(path)
        if not marker:
            return False
        try:
            return float(marker.get("expires_at", 0)) > self._clock()
        except (TypeError, ValueError):
            return False

    def marker_issues(self) -> List[str]:
        try:
            names = os.listdir(self.claims_dir)
        except OSError:
            return []
        out = []
        for name in names:
            if name.startswith("issue-") and name.endswith(".lock"):
                out.append(name[len("issue-"):-len(".lock")])
        return sorted(out)

    def stale_claims(self, ttl: int = CLAIM_TTL_SECONDS) -> List[str]:
        """Issues whose claim is no longer live (ledger or marker)."""
        now = self._clock()
        keys = set()
        for key, claim in self.data["claims"].items():
            try:
                expires = float(claim.get("expires_at", 0))
            except (TypeError, ValueError):
                expires = 0.0
            if expires <= now:
                keys.add(key)
        for key in self.marker_issues():
            if not self.marker_is_fresh(self.claim_path(key)):
                keys.add(key)
        return sorted(keys)

    def reclaim_stale(self) -> List[str]:
        stale = self.stale_claims()
        for key in stale:
            self.data["claims"].pop(key, None)
            marker = self.claim_path(key)
            if os.path.exists(marker):
                os.unlink(marker)
        return stale

    # -- budget ------------------------------------------------------------
    def usage(self, day: Optional[str] = None) -> Dict[str, float]:
        key = day or today(self._clock())
        row = self.data["usage"].get(key) or {}
        return {"issues": int(row.get("issues", 0)),
                "cost": round(float(row.get("cost", 0.0)), 6)}

    def record_usage(self, cost: float = 0.0, day: Optional[str] = None) -> Dict[str, float]:
        key = day or today(self._clock())
        row = self.data["usage"].setdefault(key, {"issues": 0, "cost": 0.0})
        row["issues"] = int(row.get("issues", 0)) + 1
        row["cost"] = round(float(row.get("cost", 0.0)) + float(cost), 6)
        return {"issues": row["issues"], "cost": row["cost"]}

    def can_start(self, issue: Any, estimate: float = DEFAULT_ISSUE_ESTIMATE,
                  max_per_issue: float = MAX_COST_PER_ISSUE,
                  max_per_day: float = MAX_COST_PER_DAY,
                  max_issues_per_day: int = MAX_ISSUES_PER_DAY) -> Tuple[bool, str]:
        """The budget/concurrency gate. Returns (allowed, reason)."""
        if self.is_paused():
            return False, "worker is PAUSED (kill switch file present)"
        if self.is_processed(issue):
            return False, f"issue {issue} already processed (idempotency guard)"
        key = str(issue)
        live = self.data["claims"].get(key)
        if live and key not in self.stale_claims():
            return False, f"issue {key} is claimed by run {live.get('run_id')}"
        if float(estimate) > float(max_per_issue):
            return False, (f"estimate ${estimate:.3f} exceeds the per-issue cap "
                           f"${max_per_issue:.3f}")
        used = self.usage()
        if used["issues"] >= int(max_issues_per_day):
            return False, (f"daily issue limit reached ({used['issues']}/"
                           f"{max_issues_per_day})")
        if used["cost"] + float(estimate) > float(max_per_day):
            return False, (f"daily budget would be exceeded "
                           f"(${used['cost']:.3f} + ${estimate:.3f} > ${max_per_day:.3f})")
        return True, "ok"

    def remaining_today(self, max_cost: float = MAX_COST_PER_DAY,
                        max_issues: int = MAX_ISSUES_PER_DAY) -> Dict[str, Any]:
        used = self.usage()
        return {
            "date": today(self._clock()),
            "issues_used": used["issues"],
            "issues_left": max(0, int(max_issues) - used["issues"]),
            "cost_used": used["cost"],
            "cost_left": round(max(0.0, float(max_cost) - used["cost"]), 6),
        }

    # -- audit -------------------------------------------------------------
    def append_audit(self, record: Dict[str, Any]) -> str:
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, AUDIT_FILE)
        row = dict(record)
        row.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        return path

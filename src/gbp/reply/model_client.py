"""Model clients for the reply generator.

``ModelClient`` is the protocol the generator depends on, which keeps the engine
model-agnostic. Two implementations ship:

* :class:`MockModelClient` -- scripted, offline, used by every unit test.
* :class:`OpenRouterModelClient` -- REAL network client (OpenRouter). It must
  never be instantiated in unit tests.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, List, Optional, Sequence, Union

try:  # pragma: no cover - typing only
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

    def runtime_checkable(cls):  # type: ignore
        return cls

__all__ = [
    "ApiError",
    "ModelError",
    "ModelClient",
    "MockModelClient",
    "OpenRouterModelClient",
    "DEFAULT_MODEL",
    "DEFAULT_BASE_URL",
]

#: The model already provisioned for this project.
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"


class ApiError(RuntimeError):
    """Raised when a model provider returns a non-success response.

    Carries an optional HTTP ``code`` so the retry policy can tell a retryable
    429/5xx from a terminal 4xx.
    """

    code: int = 0

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.code = int(code or 0)


class ModelError(ApiError):
    """Raised on the FINAL failure after the retry policy is exhausted.

    The message never includes the API key (it must never reach a log line).
    """


@runtime_checkable
class ModelClient(Protocol):
    """Anything able to complete a chat prompt."""

    def complete(self, system: str, user: str, timeout: float = 30.0) -> str:
        ...  # pragma: no cover - protocol definition


class MockModelClient:
    """Scripted in-memory client for tests (no network, ever).

    ``responses`` is a script consumed in order; the final entry repeats once
    exhausted (handy for repair flows). An entry may be:

    * ``str``            -- returned verbatim (use to simulate invalid JSON),
    * ``dict``/``list``  -- JSON-encoded before returning,
    * ``BaseException``  -- raised (simulates TimeoutError / ApiError),
    * ``callable(system, user)`` -- called and its result used.

    ``raise_on_call`` raises unconditionally on every call instead.
    """

    def __init__(
        self,
        responses: Optional[Sequence[Union[str, dict, list, BaseException, Callable]]] = None,
        raise_on_call: Optional[BaseException] = None,
        default_reply: str = "Thank you for your feedback.",
    ):
        self.responses: List[Any] = list(responses or [])
        self.raise_on_call = raise_on_call
        self.default_reply = default_reply
        #: every (system, user) prompt seen, for prompt-leak assertions.
        self.calls: List[dict] = []

    # ------------------------------------------------------------------ #
    def _next_response(self) -> Any:
        if not self.responses:
            return json.dumps({"reply": self.default_reply})
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)

    def complete(self, system: str, user: str, timeout: float = 30.0) -> str:
        self.calls.append({"system": system, "user": user, "timeout": timeout})
        if self.raise_on_call is not None:
            raise self.raise_on_call
        item = self._next_response()
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(system, user)
        if isinstance(item, str):
            return item
        return json.dumps(item)

    # ------------------------------------------------------------------ #
    @property
    def prompts(self) -> List[str]:
        """All system+user text concatenated, for leak checks."""
        return [c["system"] + "\n" + c["user"] for c in self.calls]

    def last_prompt(self) -> str:
        return self.prompts[-1] if self.calls else ""


class OpenRouterModelClient:
    """REAL OpenRouter chat-completions client (not used in unit tests).

    Uses ``OPENROUTER_API_KEY`` from the environment and the project's existing
    model. ``transport`` is injectable so a future integration test can avoid
    the network without changing this class.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        temperature: float = 0.4,
        transport: Optional[Callable[[dict, float], dict]] = None,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.temperature = temperature
        self._transport = transport
        #: Total attempts = 1 initial + up to ``max_attempts - 1`` retries (<=2).
        self.max_attempts = max(1, int(max_attempts))
        self.base_delay = float(base_delay)
        self._sleep: Callable[[float], None] = sleep or time.sleep

    # ------------------------------------------------------------------ #
    def _backoff(self, attempt: int) -> float:
        """Exponential backoff (no jitter needed for a single client)."""
        return self.base_delay * (2 ** attempt)

    def _http_transport(self, payload: dict, timeout: float) -> dict:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://localhost/gbp-integration",
                "X-Title": "gbp-integration",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            # Never include the API key; the body is the provider's message.
            raise ApiError(f"HTTP {exc.code}: {detail[:300]}", code=exc.code) from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):  # socket.timeout is a TimeoutError
                raise TimeoutError(str(reason)) from exc
            raise ApiError(f"transport error: {reason}") from exc
        except TimeoutError:  # pragma: no cover - network path
            raise
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:  # pragma: no cover - network path
            raise ApiError(f"invalid provider payload: {body[:200]}") from exc

    def complete(self, system: str, user: str, timeout: float = 30.0) -> str:
        if not self.api_key:
            raise ModelError("OPENROUTER_API_KEY is not set", code=401)
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        transport = self._transport or self._http_transport
        effective_timeout = timeout or self.timeout

        data = None
        for attempt in range(self.max_attempts):
            last = attempt == self.max_attempts - 1
            try:
                data = transport(payload, effective_timeout)
            except TimeoutError as exc:
                if last:
                    raise ModelError("model request timed out", code=408) from exc
                self._sleep(self._backoff(attempt))
                continue
            except ApiError as exc:
                code = int(getattr(exc, "code", 0) or 0)
                if code == 401:  # auth: never retry
                    raise ModelError("model provider rejected the credentials", code=401) from exc
                retryable = code == 429 or code == 408 or 500 <= code < 600
                if retryable:
                    if last:
                        raise ModelError(
                            f"model provider failed after {self.max_attempts} attempts "
                            f"(HTTP {code})",
                            code=code,
                        ) from exc
                    self._sleep(self._backoff(attempt))
                    continue
                raise ModelError(str(exc)[:200], code=code) from exc
            break

        choices = (data or {}).get("choices") or []
        if not choices:
            raise ModelError(f"no choices in response: {str(data)[:200]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelError("empty completion content")
        return content

"""Provider-neutral LLM interface, plus the Anthropic implementation.

Extraction code depends only on `LLMClient`. To change provider, write another
class with the same `complete_json` method and register it in `get_llm_client`.
Retry/backoff lives in `call_with_backoff` and works with any provider that
raises the exceptions below.
"""
import logging
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from app.core.config import settings

log = logging.getLogger(__name__)


# ---- errors ------------------------------------------------------------------

class LLMError(Exception):
    """Non-retryable provider error (bad key, bad request, refusal...)."""


class LLMRefusalError(LLMError):
    """The model declined to answer (safety). Retrying the same input won't help."""


class LLMRateLimitError(Exception):
    """Retryable: provider rate limit. `retry_after` seconds if the provider said so."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMTransientError(Exception):
    """Retryable: network error, timeout, 5xx / overloaded."""


class LLMUnavailableError(LLMError):
    """Retries ran out on rate-limit/transient errors."""


# ---- interface ---------------------------------------------------------------

@dataclass
class LLMResponse:
    text: str  # JSON text
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False  # hit the output limit; JSON is probably cut off


Message = dict  # {"role": "user" | "assistant", "content": str}


class LLMClient(ABC):
    @abstractmethod
    def complete_json(
        self,
        system: str,
        messages: list[Message],
        json_schema: dict,
        *,
        temperature: float = 0.0,
        max_tokens: int = 8000,
    ) -> LLMResponse:
        """Return JSON text that follows `json_schema`.

        Implementations must use their provider's JSON mode / structured
        output / forced tool call. They may raise LLMError, LLMRateLimitError
        or LLMTransientError.
        `temperature` is a request for deterministic output; a provider that no
        longer accepts the parameter must achieve determinism another way.
        """


# ---- Anthropic ---------------------------------------------------------------

class AnthropicLLMClient(LLMClient):
    """Claude via structured outputs (`output_config.format`).

    Anthropic's API rejects `temperature`/`top_p`/`top_k` (HTTP 400 on every
    model since API version 2026-10-21), so `temperature` is accepted but not
    sent; the JSON-schema constraint is what keeps output well-formed.
    """

    BETA_FALLBACK = "server-side-fallback-2026-07-01"

    def __init__(self, model: str | None = None):
        import anthropic

        self._anthropic = anthropic
        # max_retries=0: retrying is done once, in call_with_backoff.
        self._client = anthropic.Anthropic(
            api_key=settings.llm_api_key or None,
            max_retries=0,
            timeout=settings.llm_request_timeout_seconds,
        )
        self.model = model or settings.llm_model

    def complete_json(self, system, messages, json_schema, *, temperature=0.0, max_tokens=8000) -> LLMResponse:
        a = self._anthropic
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config={
                "effort": settings.llm_effort,
                "format": {"type": "json_schema", "schema": a.transform_schema(json_schema)},
            },
        )
        if settings.llm_use_refusal_fallbacks:
            kwargs.update(betas=[self.BETA_FALLBACK], fallbacks="default")
        try:
            resp = self._client.beta.messages.create(**kwargs)
        except a.RateLimitError as exc:
            raise LLMRateLimitError(str(exc), _retry_after(exc)) from exc
        except (a.APIConnectionError, a.APITimeoutError) as exc:
            raise LLMTransientError(f"network error: {exc}") from exc
        except a.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code == 529:
                raise LLMTransientError(f"provider error {exc.status_code}") from exc
            raise LLMError(f"LLM request rejected ({exc.status_code}): {exc.message}") from exc

        if resp.stop_reason == "refusal":
            raise LLMRefusalError("The model declined to process this document.")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return LLMResponse(
            text=text,
            model=resp.model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            truncated=resp.stop_reason == "max_tokens",
        )


def _retry_after(exc) -> float | None:
    try:
        return float(exc.response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None


def get_llm_client() -> LLMClient:
    if settings.llm_provider == "anthropic":
        return AnthropicLLMClient()
    if settings.llm_provider == "demo":
        from app.services.demo_llm import DemoLLMClient

        return DemoLLMClient()
    raise LLMError(f"Unknown LLM_PROVIDER '{settings.llm_provider}'.")


# ---- retry with backoff + shared rate-limit gate -----------------------------

class RateGate:
    """When one worker is rate-limited, all workers pause until the cool-down ends."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._lock = threading.Lock()
        self._until = 0.0
        self._clock, self._sleep = clock, sleep

    def wait(self) -> None:
        with self._lock:
            delay = self._until - self._clock()
        if delay > 0:
            self._sleep(delay)

    def cool_down(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, self._clock() + seconds)


@dataclass
class CallStats:
    attempts: int = 0
    retries: int = 0
    rate_limited: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)


def call_with_backoff(
    fn: Callable[[], LLMResponse],
    *,
    gate: RateGate,
    stats: CallStats | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int | None = None,
) -> LLMResponse:
    """Run fn(), retrying rate-limit and transient errors with exponential backoff + jitter.

    LLMError (non-retryable) propagates immediately. After the last attempt
    raises LLMUnavailableError.
    """
    max_attempts = max_attempts or settings.llm_max_attempts
    stats = stats or CallStats()
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        gate.wait()
        stats.add(attempts=1)
        try:
            resp = fn()
            stats.add(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)
            return resp
        except LLMRateLimitError as exc:
            last = exc
            stats.add(rate_limited=1)
            delay = exc.retry_after if exc.retry_after is not None else _backoff(attempt)
            gate.cool_down(delay)
        except LLMTransientError as exc:
            last = exc
            delay = _backoff(attempt)
        if attempt < max_attempts:
            stats.add(retries=1)
            log.warning("LLM call failed (%s); retry %d/%d in %.1fs", last, attempt, max_attempts - 1, delay)
            sleep(delay)
    raise LLMUnavailableError(f"The LLM service stayed unavailable after {max_attempts} attempts: {last}")


def _backoff(attempt: int) -> float:
    base = settings.llm_backoff_base_seconds * (2 ** (attempt - 1))
    return min(settings.llm_backoff_max_seconds, base) * random.uniform(0.5, 1.0)

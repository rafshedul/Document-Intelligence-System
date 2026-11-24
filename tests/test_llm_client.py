import anthropic
import httpx
import pytest

from app.core.config import settings
from app.services import llm_client as lc
from app.services.llm_client import (
    AnthropicLLMClient, CallStats, LLMError, LLMRateLimitError, LLMRefusalError, LLMResponse,
    LLMTransientError, LLMUnavailableError, RateGate, call_with_backoff,
)

OK = LLMResponse(text="{}", input_tokens=10, output_tokens=5)


def run(script, **kw):
    sleeps, stats = [], CallStats()
    it = iter(script)

    def fn():
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    gate = RateGate(sleep=sleeps.append)
    out = call_with_backoff(fn, gate=gate, stats=stats, sleep=sleeps.append, **kw)
    return out, sleeps, stats


def test_success_first_try():
    out, sleeps, stats = run([OK])
    assert out is OK and sleeps == [] and stats.attempts == 1 and stats.output_tokens == 5


def test_retries_transient_then_succeeds_with_growing_backoff():
    out, sleeps, stats = run([LLMTransientError("x"), LLMTransientError("x"), OK])
    assert out is OK and stats.retries == 2 and len(sleeps) == 2 and sleeps[1] > sleeps[0] * 0.9


def test_rate_limit_uses_retry_after_and_cools_the_gate():
    out, sleeps, stats = run([LLMRateLimitError("slow", retry_after=7.0), OK])
    assert out is OK and stats.rate_limited == 1 and 7.0 in sleeps


def test_gate_makes_other_workers_wait():
    waits = []
    now = [100.0]
    gate = RateGate(clock=lambda: now[0], sleep=waits.append)
    gate.cool_down(5)
    gate.wait()
    assert waits == [5]
    now[0] = 106
    gate.wait()
    assert waits == [5]


def test_gives_up_after_max_attempts():
    with pytest.raises(LLMUnavailableError):
        run([LLMTransientError("x")] * 3, max_attempts=3)


def test_non_retryable_error_propagates_immediately():
    with pytest.raises(LLMError):
        run([LLMError("bad key"), OK])


# ---- Anthropic adapter, with a fake SDK client ---------------------------------

class FakeMessages:
    def __init__(self, result):
        self.result, self.kwargs = result, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def adapter(result):
    c = AnthropicLLMClient.__new__(AnthropicLLMClient)
    c._anthropic, c.model = anthropic, "claude-opus-5-5"
    c._client = Obj(beta=Obj(messages=FakeMessages(result)))
    return c, c._client.beta.messages


def good(stop="end_turn"):
    return Obj(content=[Obj(type="text", text='{"a":1}')], stop_reason=stop, model="claude-opus-5-5",
               usage=Obj(input_tokens=11, output_tokens=7))


def test_anthropic_request_uses_json_schema_and_never_sends_temperature():
    c, fake = adapter(good())
    out = c.complete_json("sys", [{"role": "user", "content": "hi"}], {"type": "object", "properties": {}},
                          temperature=0.0)
    k = fake.kwargs
    assert out.text == '{"a":1}' and out.input_tokens == 11
    assert "temperature" not in k and "top_p" not in k
    assert k["output_config"]["format"]["type"] == "json_schema"
    assert k["model"] == "claude-opus-5-5" and k["system"] == "sys"
    assert "tool_choice" not in k  # forced tool use is rejected by this model


def test_refusal_fallbacks_can_be_switched_off(monkeypatch):
    c, fake = adapter(good())
    c.complete_json("s", [], {"type": "object"})
    assert fake.kwargs["fallbacks"] == "default"
    monkeypatch.setattr(settings, "llm_use_refusal_fallbacks", False)
    c.complete_json("s", [], {"type": "object"})
    assert "fallbacks" not in fake.kwargs


def http_error(cls, status, headers=None):
    req = httpx.Request("POST", "https://x")
    return cls("boom", response=httpx.Response(status, headers=headers or {}, request=req), body=None)


def test_anthropic_errors_are_mapped():
    c, _ = adapter(http_error(anthropic.RateLimitError, 429, {"retry-after": "9"}))
    with pytest.raises(LLMRateLimitError) as e:
        c.complete_json("s", [], {"type": "object"})
    assert e.value.retry_after == 9.0

    c, _ = adapter(http_error(anthropic.InternalServerError, 500))
    with pytest.raises(LLMTransientError):
        c.complete_json("s", [], {"type": "object"})

    c, _ = adapter(http_error(anthropic.AuthenticationError, 401))
    with pytest.raises(LLMError):
        c.complete_json("s", [], {"type": "object"})

    c, _ = adapter(anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")))
    with pytest.raises(LLMTransientError):
        c.complete_json("s", [], {"type": "object"})


def test_refusal_and_truncation():
    c, _ = adapter(good("refusal"))
    with pytest.raises(LLMRefusalError):
        c.complete_json("s", [], {"type": "object"})
    c, _ = adapter(good("max_tokens"))
    assert c.complete_json("s", [], {"type": "object"}).truncated


def test_unknown_provider():
    old = settings.llm_provider
    settings.llm_provider = "nope"
    try:
        with pytest.raises(LLMError):
            lc.get_llm_client()
    finally:
        settings.llm_provider = old

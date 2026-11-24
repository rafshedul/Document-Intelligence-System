"""Mock LLM for tests: no real API is ever called."""
import json
import threading

from app.services.llm_client import LLMClient, LLMResponse


def field(value=None, snippet=None, conf=0.9):
    return {"value": value, "confidence": 0.0 if value is None else conf, "source_snippet": snippet}


def response(**overrides) -> dict:
    """A valid LLM JSON object; override any field."""
    base = {k: field() for k in ("invoice_number", "invoice_date", "supplier_name", "customer_name",
                                 "subtotal", "tax", "total", "currency")}
    base.update(line_items=[], multiple_invoices_detected=False)
    base.update(overrides)
    return base


class MockLLM(LLMClient):
    """`script` items, consumed one per call: dict/str (returned), Exception (raised),
    or callable(system, messages) -> dict/str. If the script runs out, the last item repeats."""

    def __init__(self, script=None, handler=None, delay=0.0):
        self.script = list(script or [])
        self.handler = handler
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self.active = self.max_active = 0
        self.delay = delay

    def complete_json(self, system, messages, json_schema, *, temperature=0.0, max_tokens=8000):
        import time

        with self._lock:
            self.calls.append(dict(system=system, messages=messages, schema=json_schema,
                                   temperature=temperature, max_tokens=max_tokens))
            n = len(self.calls)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.handler:
                item = self.handler(system, messages)
            else:
                item = self.script[min(n - 1, len(self.script) - 1)]
                if callable(item):
                    item = item(system, messages)
            if isinstance(item, Exception):
                raise item
            text = item if isinstance(item, str) else json.dumps(item)
            return LLMResponse(text=text, model="mock-model", input_tokens=100, output_tokens=50)
        finally:
            with self._lock:
                self.active -= 1

"""A rule-based stand-in for the LLM, so the whole pipeline can be tried without an API key.

NOT an AI model: it reads simple "Label: value" invoices with regexes and returns null for
anything it cannot match. Select it with LLM_PROVIDER=demo. Its output goes through the same
Pydantic validation and snippet verification as a real model's.
"""
import json
import re

from app.services.field_verification import parse_decimal_lenient
from app.services.llm_client import LLMClient, LLMResponse

_NUM = r"-?[\d][\d.,]*\d|-?\d"
_ITEM = re.compile(rf"^(?P<d>.+?)\s+(?P<q>{_NUM})\s*[x×]\s*(?P<u>{_NUM})\s*=\s*(?P<t>{_NUM})\s*$", re.I)
CONF = 0.8


def _f(value=None, snippet=None):
    return {"value": value, "confidence": CONF if value is not None else 0.0, "source_snippet": snippet}


def _num(s: str) -> str | None:
    d = parse_decimal_lenient(s)
    return None if d is None else format(d, "f")


def extract_demo(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = {k: _f() for k in ("invoice_number", "invoice_date", "supplier_name", "customer_name",
                             "subtotal", "tax", "total", "currency")}

    def first(pattern, flags=re.I):
        for ln in lines:
            m = re.search(pattern, ln, flags)
            if m:
                return m, ln
        return None, None

    m, ln = first(r"invoice\s*(?:no\.?|number|#)\s*[:#]?\s*([A-Z0-9][\w\-/]*\d[\w\-/]*)")
    if m:
        out["invoice_number"] = _f(m.group(1), ln)
    m, ln = first(r"date\s*:\s*(\d{4}-\d{2}-\d{2})\b")
    if m:
        out["invoice_date"] = _f(m.group(1), ln)
    m, ln = first(r"^(?:supplier|vendor|seller|from)\s*:\s*(.+)$")
    if m:
        out["supplier_name"] = _f(m.group(1).strip(), ln)
    m, ln = first(r"^(?:customer|bill\s*to|buyer|client)\s*:\s*(.+)$")
    if m:
        out["customer_name"] = _f(m.group(1).strip(), ln)
    for name, pat in (("subtotal", r"^sub\s*total\b"), ("tax", r"^(?:tax|vat|gst)\b"),
                      ("total", r"^(?:grand\s+)?total\b")):
        m, ln = first(pat)
        if m:
            tail = ln.split(":", 1)[1] if ":" in ln else ln[m.end():]  # "Tax (5%): 36.05" -> "36.05"
            n = re.search(_NUM, tail)
            v = _num(n.group(0)) if n else None
            if v is not None:
                out[name] = _f(v, ln)
    cur, ln = first(r"(?i:^(?:grand\s+)?total\b).*\b([A-Z]{3})\b", 0)
    if cur:
        out["currency"] = _f(cur.group(1), ln)

    items = []
    for ln in lines:
        m = _ITEM.match(ln)
        if m:
            q, u, t = (_num(m.group(k)) for k in "qut")
            if None not in (q, u, t):
                items.append({"description": m.group("d").strip(), "quantity": q, "unit_price": u,
                              "line_total": t, "confidence": CONF, "source_snippet": ln})
    return {**out, "line_items": items, "multiple_invoices_detected": False}


class DemoLLMClient(LLMClient):
    model = "demo-rules"

    def complete_json(self, system, messages, json_schema, *, temperature=0.0, max_tokens=8000) -> LLMResponse:
        content = messages[0]["content"]
        text = content.split("<document>", 1)[-1].rsplit("</document>", 1)[0]
        return LLMResponse(text=json.dumps(extract_demo(text)), model=self.model)

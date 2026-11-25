"""Pure scoring functions for the evaluation. No database, no I/O: everything here is a function of
(ground truth, system output), so it can be unit-tested (tests/test_evaluation_metrics.py).

Definitions (also printed in report.md):

* A *field* is one of the 8 invoice fields. Truth `null` means "this document does not state it".
* Text fields (invoice_number, supplier_name, customer_name): equal after trimming, collapsing runs of
  whitespace and ignoring upper/lower case. Currency: equal after upper-casing. Date: same calendar date.
  Money (subtotal, tax, total): absolute difference <= 0.01.
* Outcome of one (invoice, field) pair:
    correct          truth has a value and the output agrees
    correct_null     truth is null and the output is null too (right to say nothing)
    missed           truth has a value, output is null
    hallucinated     truth is null, output has a value (the system invented something)
    wrong            both have a value and they differ
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

FIELDS = ("invoice_number", "invoice_date", "supplier_name", "customer_name",
          "subtotal", "tax", "total", "currency")
MONEY = ("subtotal", "tax", "total")
MONEY_TOLERANCE = Decimal("0.01")
QTY_TOLERANCE = Decimal("0.001")


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _text(v) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip().casefold()
    return s or None


def values_match(name: str, truth, output) -> bool:
    """Both null -> True. Exactly one null -> False. Otherwise compare by the rules above."""
    if truth is None or output is None:
        return truth is None and output is None
    if name in MONEY:
        a, b = _dec(truth), _dec(output)
        return a is not None and b is not None and abs(a - b) <= MONEY_TOLERANCE
    if name == "invoice_date":
        return _as_date(truth) is not None and _as_date(truth) == _as_date(output)
    if name == "currency":
        return str(truth).strip().upper() == str(output).strip().upper()
    return _text(truth) == _text(output)


def _as_date(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return None


def classify(name: str, truth, output) -> str:
    """'correct' | 'correct_null' | 'missed' | 'hallucinated' | 'wrong' (see module docstring)."""
    if values_match(name, truth, output):
        return "correct_null" if truth is None else "correct"
    if output is None:
        return "missed"
    if truth is None:
        return "hallucinated"
    return "wrong"


# ---- matching extracted invoices to labelled ones -----------------------------------------

def match_invoices(truth: list[dict], output: list[dict]) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Pair truth invoices with extracted ones. First by invoice number (case/space-insensitive),
    then the leftovers by position. Returns (pairs[(truth_i, output_i)], unmatched_truth, unmatched_output)."""
    pairs: list[tuple[int, int]] = []
    used_t: set[int] = set()
    used_o: set[int] = set()
    for ti, t in enumerate(truth):
        tn = _text(t.get("invoice_number"))
        if tn is None:
            continue
        for oi, o in enumerate(output):
            if oi not in used_o and _text(o.get("invoice_number")) == tn:
                pairs.append((ti, oi))
                used_t.add(ti)
                used_o.add(oi)
                break
    rest_t = [i for i in range(len(truth)) if i not in used_t]
    rest_o = [i for i in range(len(output)) if i not in used_o]
    for ti, oi in zip(rest_t, rest_o):
        pairs.append((ti, oi))
        used_t.add(ti)
        used_o.add(oi)
    pairs.sort()
    return (pairs, [i for i in range(len(truth)) if i not in used_t],
            [i for i in range(len(output)) if i not in used_o])


# ---- line items ---------------------------------------------------------------------------

def item_matches(t: dict, o: dict) -> bool:
    """Same description (text rule) and the same quantity, unit price and line total."""
    if _text(t.get("description")) != _text(o.get("description")):
        return False
    for key, tol in (("quantity", QTY_TOLERANCE), ("unit_price", MONEY_TOLERANCE), ("line_total", MONEY_TOLERANCE)):
        a, b = _dec(t.get(key)), _dec(o.get(key))
        if (a is None) != (b is None):
            return False
        if a is not None and abs(a - b) > tol:
            return False
    return True


def count_item_matches(truth: list[dict], output: list[dict]) -> int:
    """One-to-one matching: each truth item can be matched by at most one output item."""
    used: set[int] = set()
    n = 0
    for t in truth:
        for oi, o in enumerate(output):
            if oi not in used and item_matches(t, o):
                used.add(oi)
                n += 1
                break
    return n


# ---- aggregation ----------------------------------------------------------------------------

def ratio(num: int | float, den: int | float) -> float | None:
    """None (not 0) when there is nothing to divide by, so the report says 'n/a' instead of a made-up 0%."""
    return None if not den else num / den


@dataclass
class DocScore:
    """What one successfully processed document contributes."""

    field_outcomes: dict[str, dict[str, int]] = field(default_factory=dict)  # field -> outcome -> count
    invoices_truth: int = 0
    invoices_output: int = 0
    invoices_matched: int = 0
    invoices_all_fields_correct: int = 0
    invoices_all_correct_with_items: int = 0
    item_truth: int = 0
    item_output: int = 0
    item_matched: int = 0


def score_document(truth: list[dict], output: list[dict]) -> DocScore:
    """Score one document: `truth` and `output` are lists of invoice dicts (8 fields + `line_items`).
    A truth invoice nobody extracted counts every field as missed/correct-null as if the output were all null."""
    s = DocScore(invoices_truth=len(truth), invoices_output=len(output))
    pairs, unmatched_t, unmatched_o = match_invoices(truth, output)
    s.invoices_matched = len(pairs)
    empty = {k: None for k in FIELDS} | {"line_items": []}
    work = [(truth[ti], output[oi]) for ti, oi in pairs] + [(truth[ti], empty) for ti in unmatched_t]
    s.item_output = sum(len(o.get("line_items") or []) for _, o in work) + \
        sum(len(output[oi].get("line_items") or []) for oi in unmatched_o)
    for t, o in work:
        all_ok = True
        for name in FIELDS:
            outcome = classify(name, t.get(name), o.get(name))
            bucket = s.field_outcomes.setdefault(name, {})
            bucket[outcome] = bucket.get(outcome, 0) + 1
            all_ok &= outcome in ("correct", "correct_null")
        titems, oitems = t.get("line_items") or [], o.get("line_items") or []
        m = count_item_matches(titems, oitems)
        s.item_truth += len(titems)
        s.item_matched += m
        s.invoices_all_fields_correct += all_ok
        s.invoices_all_correct_with_items += all_ok and m == len(titems) == len(oitems)
    return s


def summarize(scores: list[DocScore]) -> dict:
    """Combine document scores into the numbers of the report. Every ratio carries its counts."""
    kinds = ("correct", "correct_null", "missed", "hallucinated", "wrong")
    fields: dict[str, dict] = {}
    tot = dict.fromkeys(kinds, 0)
    for name in FIELDS:
        c = dict.fromkeys(kinds, 0)
        for s in scores:
            for k, v in s.field_outcomes.get(name, {}).items():
                c[k] += v
        labelled = c["correct"] + c["missed"] + c["wrong"]  # fields whose truth has a value
        fields[name] = {**c, "labelled_with_value": labelled, "accuracy": ratio(c["correct"], labelled)}
        for k in kinds:
            tot[k] += c[k]
    truth_value = tot["correct"] + tot["missed"] + tot["wrong"]
    truth_null = tot["correct_null"] + tot["hallucinated"]
    out_value = tot["correct"] + tot["wrong"] + tot["hallucinated"]
    out_null = tot["correct_null"] + tot["missed"]
    inv_t = sum(s.invoices_truth for s in scores)
    ok = sum(s.invoices_all_fields_correct for s in scores)
    ok_items = sum(s.invoices_all_correct_with_items for s in scores)
    it_t, it_o, it_m = (sum(getattr(s, k) for s in scores) for k in ("item_truth", "item_output", "item_matched"))
    return {
        "fields": fields,
        "invoice_accuracy": {"correct": ok, "total": inv_t, "rate": ratio(ok, inv_t)},
        "invoice_accuracy_with_line_items": {"correct": ok_items, "total": inv_t, "rate": ratio(ok_items, inv_t)},
        "invoices": {"labelled": inv_t, "extracted": sum(s.invoices_output for s in scores),
                     "matched": sum(s.invoices_matched for s in scores)},
        "line_items": {"labelled": it_t, "extracted": it_o, "matched": it_m,
                       "precision": ratio(it_m, it_o), "recall": ratio(it_m, it_t)},
        # Of the fields the document states: how many did the system leave empty?
        "null_rate": {"missed": tot["missed"], "of_labelled_values": truth_value,
                      "rate": ratio(tot["missed"], truth_value),
                      "null_outputs": out_null, "of_all_fields": out_null + out_value,
                      "overall_null_share": ratio(out_null, out_null + out_value)},
        # Of the fields the document does NOT state: how many did the system invent a value for?
        "hallucination": {"invented": tot["hallucinated"], "of_labelled_null_fields": truth_null,
                          "rate": ratio(tot["hallucinated"], truth_null)},
        # Of the values the system did output: how many are wrong or invented?
        "wrong_value_rate": {"wrong_or_invented": tot["wrong"] + tot["hallucinated"],
                             "of_values_output": out_value,
                             "rate": ratio(tot["wrong"] + tot["hallucinated"], out_value)},
    }


# ---- validation -----------------------------------------------------------------------------

def validation_keys(findings: list[tuple[str, str | None]], ignore: set[str]) -> set[str]:
    """('MISSING_FIELD','customer_name') -> 'MISSING_FIELD:customer_name'; other rules -> just the code."""
    out = set()
    for code, fld in findings:
        if code in ignore:
            continue
        out.add(f"{code}:{fld}" if code == "MISSING_FIELD" and fld else code)
    return out


# The fields the MISSING_FIELD rule looks at (docs/validation.md): 4 required + customer and currency.
# subtotal and tax are not checked by that rule, so their absence is not expected to be reported.
MISSING_FIELD_CHECKED = ("invoice_number", "invoice_date", "supplier_name", "total", "customer_name", "currency")


def expected_validation(truth_invoices: list[dict], listed: list[str], ignore: set[str]) -> set[str]:
    """What the validator should report for a document: MISSING_FIELD for every checked field the truth says is
    absent, plus whatever the dataset author listed (planted errors)."""
    keys = {k for k in listed if k.split(":")[0] not in ignore}
    for inv in truth_invoices:
        for name in MISSING_FIELD_CHECKED:
            if inv.get(name) is None:
                keys.add(f"MISSING_FIELD:{name}")
    return keys

"""The evaluation's scoring functions, checked against small hand-counted examples."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "evaluation"))
import metrics as m  # noqa: E402
import run_evaluation as ev  # noqa: E402


def inv(**kw):
    base = {k: None for k in m.FIELDS} | {"line_items": []}
    return base | kw


FULL = inv(invoice_number="A-1", invoice_date="2026-01-02", supplier_name="Acme Ltd", customer_name="Bob",
           subtotal="100.00", tax="5.00", total="105.00", currency="USD")


# ---- value comparison ---------------------------------------------------------------------

@pytest.mark.parametrize("name,truth,out,expected", [
    ("total", "105.00", "105.01", True),       # exactly 0.01 apart: within tolerance
    ("total", "105.00", "105.02", False),
    ("total", "105.00", "105", True),
    ("supplier_name", "Acme  Ltd", "acme ltd", True),   # case and spacing ignored
    ("supplier_name", "Acme Ltd", "Acme Limited", False),
    ("invoice_date", "2026-01-02", "2026-01-02", True),
    ("invoice_date", "2026-01-02", "2026-02-01", False),
    ("currency", "usd", "USD", True),
    ("tax", None, None, True),
    ("tax", None, "0.00", False),               # inventing a zero is still inventing
    ("tax", "0.00", None, False),
])
def test_values_match(name, truth, out, expected):
    assert m.values_match(name, truth, out) is expected


@pytest.mark.parametrize("truth,out,kind", [
    ("5", "5", "correct"), (None, None, "correct_null"), ("5", None, "missed"),
    (None, "5", "hallucinated"), ("5", "6", "wrong"),
])
def test_classify(truth, out, kind):
    assert m.classify("total", truth, out) == kind


# ---- pairing and line items ---------------------------------------------------------------

def test_invoices_are_paired_by_number_then_position():
    t = [inv(invoice_number="A"), inv(invoice_number="B"), inv(invoice_number=None)]
    o = [inv(invoice_number="b"), inv(invoice_number="Z"), inv(invoice_number="A")]
    pairs, ut, uo = m.match_invoices(t, o)
    assert pairs == [(0, 2), (1, 0), (2, 1)] and ut == [] and uo == []


def test_unpaired_invoices_are_reported():
    pairs, ut, uo = m.match_invoices([inv(invoice_number="A")], [inv(invoice_number="A"), inv(invoice_number="B")])
    assert pairs == [(0, 0)] and ut == [] and uo == [1]


def test_line_items_need_every_part_to_match_and_count_once():
    item = {"description": "Pen", "quantity": "2", "unit_price": "1.50", "line_total": "3.00"}
    assert m.item_matches(item, dict(item, description="PEN"))
    assert not m.item_matches(item, dict(item, line_total="3.05"))
    assert not m.item_matches(item, dict(item, quantity=None))
    # one output item cannot satisfy two truth items
    assert m.count_item_matches([item, item], [item]) == 1


# ---- whole-document and aggregate scores --------------------------------------------------

def test_perfect_document():
    s = m.summarize([m.score_document([FULL], [dict(FULL)])])
    assert s["invoice_accuracy"] == {"correct": 1, "total": 1, "rate": 1.0}
    assert all(f["accuracy"] == 1.0 for f in s["fields"].values())
    assert s["hallucination"]["rate"] is None      # nothing labelled null: n/a, not 0%
    assert s["line_items"]["precision"] is None and s["line_items"]["recall"] is None


def test_counts_by_hand():
    truth = [inv(invoice_number="A-1", invoice_date="2026-01-02", total="10.00", supplier_name="Acme")]
    out = [inv(invoice_number="A-1", invoice_date=None,            # missed
               total="11.00",                                      # wrong
               supplier_name="Acme",                               # correct
               customer_name="Bob")]                               # invented (truth null)
    s = m.summarize([m.score_document(truth, out)])
    f = s["fields"]
    assert (f["invoice_number"]["correct"], f["invoice_date"]["missed"], f["total"]["wrong"]) == (1, 1, 1)
    assert f["customer_name"]["hallucinated"] == 1
    assert s["invoice_accuracy"]["correct"] == 0
    # truth has values for 4 fields (number, date, supplier, total): 1 missed -> 25%
    assert s["null_rate"] == pytest.approx({**s["null_rate"], "missed": 1, "of_labelled_values": 4, "rate": 0.25})
    # truth is null for the other 4 fields; the system invented 1 of them
    assert (s["hallucination"]["invented"], s["hallucination"]["of_labelled_null_fields"]) == (1, 4)
    # it gave 4 values (number, total, supplier, customer): 2 of them are wrong/invented
    assert (s["wrong_value_rate"]["wrong_or_invented"], s["wrong_value_rate"]["of_values_output"]) == (2, 4)


def test_missing_invoice_counts_as_missed_not_skipped():
    s = m.summarize([m.score_document([FULL], [])])
    assert s["fields"]["total"]["missed"] == 1
    assert s["invoices"] == {"labelled": 1, "extracted": 0, "matched": 0}


def test_line_item_precision_and_recall():
    a = {"description": "A", "quantity": "1", "unit_price": "1.00", "line_total": "1.00"}
    b = {"description": "B", "quantity": "1", "unit_price": "2.00", "line_total": "2.00"}
    c = {"description": "C", "quantity": "1", "unit_price": "3.00", "line_total": "3.00"}
    s = m.summarize([m.score_document([inv(line_items=[a, b, c])], [inv(line_items=[a, b, dict(c, line_total="9")])])])
    li = s["line_items"]
    assert (li["labelled"], li["extracted"], li["matched"]) == (3, 3, 2)
    assert li["precision"] == pytest.approx(2 / 3) and li["recall"] == pytest.approx(2 / 3)
    assert s["invoice_accuracy_with_line_items"]["correct"] == 0


# ---- validation keys ----------------------------------------------------------------------

def test_expected_validation_is_derived_from_nulls_plus_listed_codes():
    exp = m.expected_validation([inv(invoice_number="A", total="1")], ["TOTAL_MISMATCH", "LOW_CONFIDENCE"],
                                {"LOW_CONFIDENCE"})
    assert "TOTAL_MISMATCH" in exp and "LOW_CONFIDENCE" not in exp
    assert "MISSING_FIELD:invoice_date" in exp and "MISSING_FIELD:total" not in exp
    assert "MISSING_FIELD:tax" not in exp  # the rule does not check tax or subtotal


def test_validation_keys_ignore_confidence_and_name_missing_fields():
    keys = m.validation_keys([("MISSING_FIELD", "currency"), ("LOW_CONFIDENCE", "total"),
                              ("TOTAL_MISMATCH", "total")], {"LOW_CONFIDENCE"})
    assert keys == {"MISSING_FIELD:currency", "TOTAL_MISMATCH"}


# ---- ground-truth file checks -------------------------------------------------------------

def _write(tmp_path, docs):
    (tmp_path / "ground_truth.json").write_text(json.dumps({"format_version": 1, "documents": docs}))


def test_truth_file_must_state_every_field(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"x")
    bad = {k: None for k in m.FIELDS}
    del bad["tax"]
    _write(tmp_path, {"a.pdf": {"invoices": [bad]}})
    with pytest.raises(ev.TruthError, match="missing 'tax'"):
        ev.load_truth(str(tmp_path))


def test_truth_file_rejects_missing_files_and_bad_numbers(tmp_path):
    _write(tmp_path, {"gone.pdf": {"invoices": [{k: None for k in m.FIELDS} | {"total": "12,5"}]}})
    with pytest.raises(ev.TruthError) as e:
        ev.load_truth(str(tmp_path))
    assert "file not found" in str(e.value) and "not a number" in str(e.value)


def test_the_shipped_sample_truth_is_valid():
    truth = ev.load_truth(os.path.join(os.path.dirname(__file__), "..", "evaluation", "labeled_dataset"))
    assert len(truth["documents"]) >= 10

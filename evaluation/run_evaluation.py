"""Run every labelled document through the real pipeline and measure the result.

    EVAL_DATABASE_URL=postgresql://USER@HOST:PORT/DBNAME  python evaluation/run_evaluation.py [--reset]

What "real pipeline" means: each file is detected, stored and recorded as a Document exactly as the upload
endpoint does it, then handed to `run_pipeline()`, the function the Celery worker calls (text extraction or
OCR -> LLM extraction -> verification -> validation -> embeddings). The only difference is that it runs in this
process instead of through Redis. The LLM is whatever LLM_PROVIDER / LLM_API_KEY say in your environment or
.env; the report states which one it was. With LLM_PROVIDER=demo the "model" is a regex stand-in, not an AI.

Nothing in the report is typed in by hand: every number is computed from (a) your ground_truth.json and
(b) the rows the pipeline wrote to the database during this run. Results go to evaluation/report.md and
evaluation/results.json.

SAFETY: the run needs an empty database (duplicate checks look at every earlier document). Point
EVAL_DATABASE_URL at a throw-away database. `--reset` deletes all rows of its `documents` table first;
the script never reads DATABASE_URL, so it cannot touch your normal database by accident.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import metrics  # noqa: E402

FORMAT_VERSION = 1
# Rules whose output is not scored against ground truth: confidence is the model's own opinion, not a fact.
VALIDATION_IGNORE = {"LOW_CONFIDENCE"}


# ---- ground truth -------------------------------------------------------------------------

class TruthError(Exception):
    pass


def load_truth(dataset_dir: str) -> dict:
    """Read and strictly check ground_truth.json. Raises TruthError with every problem found."""
    path = os.path.join(dataset_dir, "ground_truth.json")
    if not os.path.exists(path):
        raise TruthError(f"{path} not found. See evaluation/labeled_dataset/README.md for the format.")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise TruthError(f"{path} is not valid JSON: {exc}") from exc
    problems: list[str] = []
    if data.get("format_version") != FORMAT_VERSION:
        problems.append(f"format_version must be {FORMAT_VERSION}")
    docs = data.get("documents")
    if not isinstance(docs, dict) or not docs:
        raise TruthError("'documents' must be a non-empty object {filename: {...}}")
    for name, d in docs.items():
        where = f"[{name}]"
        if not os.path.exists(os.path.join(dataset_dir, name)):
            problems.append(f"{where} file not found in {dataset_dir}")
        if not isinstance(d, dict):
            problems.append(f"{where} must be an object")
            continue
        failure = d.get("expected_failure", False)
        if not isinstance(failure, bool):
            problems.append(f"{where} expected_failure must be true or false")
        invoices = d.get("invoices")
        if not isinstance(invoices, list):
            problems.append(f"{where} 'invoices' must be a list")
            continue
        if not failure and not invoices:
            problems.append(f"{where} has no invoices and is not marked expected_failure")
        for i, inv in enumerate(invoices):
            for fld in metrics.FIELDS:
                if fld not in inv:
                    problems.append(f"{where} invoice {i}: missing '{fld}' (use null if the document does not state it)")
            for fld in metrics.MONEY:
                v = inv.get(fld)
                if v is not None and metrics._dec(v) is None:
                    problems.append(f"{where} invoice {i}: '{fld}' is not a number: {v!r}")
            if inv.get("invoice_date") is not None and metrics._as_date(inv["invoice_date"]) is None:
                problems.append(f"{where} invoice {i}: invoice_date must be YYYY-MM-DD or null")
            for j, it in enumerate(inv.get("line_items") or []):
                for k in ("description", "quantity", "unit_price", "line_total"):
                    if k not in it:
                        problems.append(f"{where} invoice {i} line {j}: missing '{k}'")
        if not isinstance(d.get("expected_validation", []), list):
            problems.append(f"{where} expected_validation must be a list of rule codes")
    if problems:
        raise TruthError("ground_truth.json has problems:\n  - " + "\n  - ".join(problems))
    return data


# ---- running ------------------------------------------------------------------------------

def run_documents(dataset_dir: str, truth: dict, names: list[str], upload_dir: str) -> list[dict]:
    """Process each file once, in name order, and read back what the pipeline stored."""
    import hashlib

    from sqlalchemy import func, select

    from app.core import db
    from app.models import Document, DocumentPage, DocumentStatus, ExtractedResult, ProcessingHistory
    from app.models import ValidationError as ValidationRow
    from app.services import file_router
    from app.services.exceptions import DocumentProcessingError
    from app.services.orchestrator import run_pipeline
    from app.services.validation_service import OWNED_CODES

    records = []
    for n, name in enumerate(names, start=1):
        src = os.path.join(dataset_dir, name)
        print(f"[{n}/{len(names)}] {name} ...", end=" ", flush=True)
        rec: dict = {"file": name, "expected_failure": truth["documents"][name].get("expected_failure", False),
                     "file_type": os.path.splitext(name)[1].lstrip(".").lower(), "kind": None,
                     "status": None, "error_message": None, "rejected_at_upload": False, "seconds": None,
                     "page_count": None, "ocr_confidence_mean": None, "stage_ms": {}, "invoices": [],
                     "findings": []}
        stored = os.path.join(upload_dir, uuid.uuid4().hex)
        shutil.copyfile(src, stored)
        try:  # the same check the upload endpoint makes: reject mislabelled / empty / encrypted files
            detection = file_router.detect(stored, name)
        except DocumentProcessingError as exc:
            rec.update(status="rejected", rejected_at_upload=True, error_message=f"{exc.code}: {exc.message}")
            records.append(rec)
            print("rejected at upload")
            continue
        with open(stored, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        with db.SessionLocal() as s:
            doc = Document(original_filename=name, stored_path=stored, file_type=detection.format,
                           file_size=os.path.getsize(stored), file_hash=digest, status=DocumentStatus.queued)
            s.add(doc)
            s.commit()
            doc_id = doc.id
        t0 = time.perf_counter()
        try:
            result = run_pipeline(doc_id)
            crashed = None if result.ok or result.error is None else result.error
        except Exception:  # run_pipeline should never raise; if it does, that is a finding too
            crashed = traceback.format_exc()[-500:]
        rec["seconds"] = time.perf_counter() - t0

        with db.SessionLocal() as s:
            doc = s.get(Document, doc_id)
            rec.update(status=doc.status.value, error_message=doc.error_message or crashed,
                       file_type=doc.file_type, page_count=doc.page_count)
            pages = s.execute(select(DocumentPage.extraction_method, DocumentPage.ocr_confidence)
                              .where(DocumentPage.document_id == doc_id)).all()
            ocr_conf = [float(p.ocr_confidence) for p in pages if p.ocr_confidence is not None]
            if ocr_conf:
                rec["ocr_confidence_mean"] = round(statistics.fmean(ocr_conf), 1)
            is_ocr = any(p.extraction_method.value == "ocr" for p in pages)
            rec["kind"] = f"{doc.file_type} (OCR)" if is_ocr else doc.file_type
            for h in s.scalars(select(ProcessingHistory).where(ProcessingHistory.document_id == doc_id)):
                if h.duration_ms is not None:
                    rec["stage_ms"][h.stage] = rec["stage_ms"].get(h.stage, 0) + h.duration_ms
            latest = s.scalar(select(func.max(ExtractedResult.version))
                              .where(ExtractedResult.document_id == doc_id))
            if latest is not None:
                for r in s.scalars(select(ExtractedResult).where(ExtractedResult.document_id == doc_id,
                                                                 ExtractedResult.version == latest)
                                   .order_by(ExtractedResult.invoice_index)):
                    rec["invoices"].append({
                        "invoice_number": r.invoice_number,
                        "invoice_date": r.invoice_date.isoformat() if r.invoice_date else None,
                        "supplier_name": r.supplier_name, "customer_name": r.customer_name,
                        "subtotal": _s(r.subtotal), "tax": _s(r.tax), "total": _s(r.total),
                        "currency": r.currency,
                        "line_items": [{"description": li.description, "quantity": _s(li.quantity),
                                        "unit_price": _s(li.unit_price), "line_total": _s(li.line_total)}
                                       for li in r.line_items]})
            for v in s.scalars(select(ValidationRow).where(ValidationRow.document_id == doc_id,
                                                           ValidationRow.resolved.is_(False))):
                rec["findings"].append({"code": v.rule_code, "severity": v.severity.value, "field": v.field,
                                        "scored": v.rule_code in OWNED_CODES and v.rule_code not in VALIDATION_IGNORE})
        records.append(rec)
        print(f"{rec['status']} in {rec['seconds']:.1f}s")
    return records


def _s(v):
    return None if v is None else format(v, "f")


# ---- scoring ------------------------------------------------------------------------------

def score(records: list[dict], truth: dict) -> dict:
    docs = truth["documents"]
    total = len(records)
    expected_ok = [r for r in records if not r["expected_failure"]]
    expected_fail = [r for r in records if r["expected_failure"]]
    # "produced a result" = the pipeline got through without failing the document (completed or needs_review).
    produced = [r for r in expected_ok if r["status"] in ("completed", "needs_review")]
    failed_unexpectedly = [r for r in expected_ok if r["status"] not in ("completed", "needs_review")]
    status_counts: dict[str, int] = {}
    for r in records:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    outcome = {
        "documents": total,
        "status_counts": status_counts,
        "expected_to_succeed": len(expected_ok),
        "success": {"count": len(produced), "of": len(expected_ok),
                    "rate": metrics.ratio(len(produced), len(expected_ok))},
        "failure": {"count": len(failed_unexpectedly), "of": len(expected_ok),
                    "rate": metrics.ratio(len(failed_unexpectedly), len(expected_ok)),
                    "files": [{"file": r["file"], "status": r["status"], "error": r["error_message"]}
                              for r in failed_unexpectedly]},
        "expected_failures_handled": {"count": sum(r["status"] in ("failed", "rejected") for r in expected_fail),
                                      "of": len(expected_fail)},
        "needs_review": {"count": sum(r["status"] == "needs_review" for r in produced), "of": len(produced)},
    }
    stray = [r["file"] for r in expected_fail if r["status"] not in ("failed", "rejected")]
    outcome["expected_failures_not_failed"] = stray

    # field-level scores over documents that produced a result
    doc_scores = []
    per_doc = {}
    for r in produced:
        t = docs[r["file"]]["invoices"]
        sc = metrics.score_document(t, r["invoices"])
        doc_scores.append(sc)
        diffs = _diffs(t, r["invoices"])
        per_doc[r["file"]] = {"fields_correct": sum(v.get("correct", 0) + v.get("correct_null", 0)
                                                    for v in sc.field_outcomes.values()),
                              "fields_total": sum(sum(v.values()) for v in sc.field_outcomes.values()),
                              "differences": diffs}
    extraction = metrics.summarize(doc_scores)
    extraction["documents_scored"] = len(doc_scores)

    # processing time by kind of file (only documents that actually ran)
    by_kind: dict[str, list[dict]] = {}
    for r in records:
        if r["seconds"] is not None and r["status"] in ("completed", "needs_review"):
            by_kind.setdefault(r["kind"], []).append(r)
    timing = {}
    for kind, rs in sorted(by_kind.items()):
        secs = [r["seconds"] for r in rs]
        stage_names = sorted({k for r in rs for k in r["stage_ms"]})
        timing[kind] = {"documents": len(rs), "mean_s": statistics.fmean(secs), "max_s": max(secs),
                        "slowest": max(rs, key=lambda r: r["seconds"])["file"],
                        "mean_stage_s": {k: statistics.fmean(r["stage_ms"].get(k, 0) for r in rs) / 1000
                                         for k in stage_names}}
    all_secs = [r["seconds"] for rs in by_kind.values() for r in rs]
    timing_all = ({"documents": len(all_secs), "mean_s": statistics.fmean(all_secs), "max_s": max(all_secs)}
                  if all_secs else None)

    # validation
    tp = fp = fn = planted_found = planted_total = clean_docs = clean_false_alarms = 0
    v_docs = {}
    for r in produced:
        d = docs[r["file"]]
        listed = [k for k in d.get("expected_validation", []) if k.split(":")[0] not in VALIDATION_IGNORE]
        expected = metrics.expected_validation(d["invoices"], d.get("expected_validation", []), VALIDATION_IGNORE)
        actual = metrics.validation_keys([(f["code"], f["field"]) for f in r["findings"] if f["scored"]],
                                         VALIDATION_IGNORE)
        tp += len(expected & actual)
        fp += len(actual - expected)
        fn += len(expected - actual)
        planted_total += len(listed)
        planted_found += len(set(listed) & actual)
        if not expected:
            clean_docs += 1
            clean_false_alarms += bool(actual)
        if expected != actual:
            v_docs[r["file"]] = {"missed": sorted(expected - actual), "unexpected": sorted(actual - expected)}
    validation = {
        "precision": {"value": metrics.ratio(tp, tp + fp), "tp": tp, "reported": tp + fp},
        "recall": {"value": metrics.ratio(tp, tp + fn), "tp": tp, "expected": tp + fn},
        "planted_errors_caught": {"found": planted_found, "of": planted_total,
                                  "rate": metrics.ratio(planted_found, planted_total)},
        "clean_documents_with_false_alarms": {"count": clean_false_alarms, "of": clean_docs,
                                              "rate": metrics.ratio(clean_false_alarms, clean_docs)},
        "documents_with_differences": v_docs,
        "ignored_rules": sorted(VALIDATION_IGNORE),
    }
    return {"outcome": outcome, "extraction": extraction, "timing": timing, "timing_all": timing_all,
            "validation": validation, "per_document": per_doc}


def _diffs(truth: list[dict], output: list[dict]) -> list[dict]:
    pairs, unmatched_t, unmatched_o = metrics.match_invoices(truth, output)
    out = []
    for ti, oi in pairs:
        for name in metrics.FIELDS:
            kind = metrics.classify(name, truth[ti].get(name), output[oi].get(name))
            if kind not in ("correct", "correct_null"):
                out.append({"invoice": ti, "field": name, "kind": kind, "truth": truth[ti].get(name),
                            "output": output[oi].get(name)})
        titems, oitems = truth[ti].get("line_items") or [], output[oi].get("line_items") or []
        m = metrics.count_item_matches(titems, oitems)
        if m != len(titems) or m != len(oitems):
            out.append({"invoice": ti, "field": "line_items", "kind": "mismatch",
                        "truth": f"{len(titems)} items", "output": f"{len(oitems)} items, {m} matched"})
    for ti in unmatched_t:
        out.append({"invoice": ti, "field": "(whole invoice)", "kind": "not extracted",
                    "truth": truth[ti].get("invoice_number"), "output": None})
    for oi in unmatched_o:
        out.append({"invoice": None, "field": "(whole invoice)", "kind": "extra invoice", "truth": None,
                    "output": output[oi].get("invoice_number")})
    return out


# ---- report -------------------------------------------------------------------------------

def pct(rate) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def frac(num, den) -> str:
    return f"{pct(metrics.ratio(num, den))} ({num}/{den})"


def render_report(meta: dict, res: dict, records: list[dict]) -> str:
    L: list[str] = []
    o, e, v = res["outcome"], res["extraction"], res["validation"]
    L += ["# Evaluation report", "",
          f"Run at {meta['started']} on commit `{meta['commit']}`. "
          f"Dataset: **{meta['documents']} documents** (`{meta['dataset']}`). "
          f"LLM provider: **{meta['llm_provider']}**, model: **{meta['llm_model']}**. "
          f"OCR languages: {meta['ocr_languages']}. Embeddings: {meta['embedding_provider']}. "
          f"Total run time {meta['wall_seconds']:.1f} s.", ""]
    if meta["llm_provider"] == "demo":
        L += ["> **Warning: this run used the `demo` extractor, which is a regex stand-in and NOT an AI model.** "
              "These numbers describe the pipeline around it (text extraction, OCR, verification, validation, "
              "timing) and say nothing about how an LLM would score. Re-run with `LLM_PROVIDER=anthropic` and a "
              "key for real extraction numbers.", ""]
    if meta["documents"] < 20:
        L += [f"> **Small sample: {meta['documents']} documents.** A rate over so few documents moves by several "
              "points when a single document changes. Treat these as a smoke test, not a benchmark (the target "
              "is 20-30 labelled documents).", ""]
    if meta.get("synthetic"):
        L += ["> The dataset is the **synthetic sample** produced by `evaluation/make_sample_dataset.py` "
              "(clean, computer-generated pages). Real scans and real layouts are harder; do not read these "
              "numbers as expected real-world accuracy.", ""]

    L += ["## Summary", "",
          "| Measure | Result |", "|---|---|",
          f"| Documents that produced a result | {frac(o['success']['count'], o['success']['of'])} |",
          f"| Documents that failed (not expected to) | {frac(o['failure']['count'], o['failure']['of'])} |",
          f"| Broken files that were rejected or failed cleanly | {o['expected_failures_handled']['count']}/"
          f"{o['expected_failures_handled']['of']} |",
          f"| Produced a result but sent to review | {frac(o['needs_review']['count'], o['needs_review']['of'])} |",
          f"| Invoice accuracy (all 8 fields right) | {frac(e['invoice_accuracy']['correct'], e['invoice_accuracy']['total'])} |",
          f"| Invoice accuracy incl. line items | "
          f"{frac(e['invoice_accuracy_with_line_items']['correct'], e['invoice_accuracy_with_line_items']['total'])} |",
          f"| Line items: precision / recall | {pct(e['line_items']['precision'])} / {pct(e['line_items']['recall'])} |",
          f"| Null rate (field has a value, system said null) | "
          f"{frac(e['null_rate']['missed'], e['null_rate']['of_labelled_values'])} |",
          f"| Hallucination rate (field has no value, system invented one) | "
          f"{frac(e['hallucination']['invented'], e['hallucination']['of_labelled_null_fields'])} |",
          f"| Wrong or invented share of the values it gave | "
          f"{frac(e['wrong_value_rate']['wrong_or_invented'], e['wrong_value_rate']['of_values_output'])} |",
          f"| Validation precision / recall | {pct(v['precision']['value'])} / {pct(v['recall']['value'])} |",
          f"| Planted errors caught by validation | {frac(v['planted_errors_caught']['found'], v['planted_errors_caught']['of'])} |",
          ""]
    L += [f"Extraction metrics cover the {e['documents_scored']} documents that produced a result. "
          f"Documents that failed are counted in the success/failure rows only.", ""]

    L += ["## Field accuracy", "",
          "Accuracy = correct / fields the document really states. Money within 0.01; text ignores case and "
          "extra spaces; dates must be the same day.", "",
          "| Field | Accuracy | Correct | Wrong | Missed (null) | Invented (truth is null) | Correctly null |",
          "|---|---|---|---|---|---|---|"]
    for name in metrics.FIELDS:
        f = e["fields"][name]
        L.append(f"| {name} | {pct(f['accuracy'])} ({f['correct']}/{f['labelled_with_value']}) | {f['correct']} | "
                 f"{f['wrong']} | {f['missed']} | {f['hallucinated']} | {f['correct_null']} |")
    li = e["line_items"]
    L += ["", f"Invoices: {e['invoices']['labelled']} labelled, {e['invoices']['extracted']} extracted, "
              f"{e['invoices']['matched']} paired with a labelled one.",
          f"Line items: {li['labelled']} labelled, {li['extracted']} extracted, {li['matched']} matched exactly "
          f"(description, quantity, unit price, line total). Precision {pct(li['precision'])}, "
          f"recall {pct(li['recall'])}.", ""]

    L += ["## Processing time", "",
          "Wall-clock seconds per document, from the start of the pipeline to the end of the embedding stage "
          "(one document at a time, one process), for documents that produced a result. Stage columns are mean "
          "seconds. There is no warm-up run: the first document processed also pays one-time costs "
          "(imports, connections).", ""]
    stages = sorted({k for t in res["timing"].values() for k in t["mean_stage_s"]})
    L += ["| File type | Docs | Mean | Max | Slowest | " + " | ".join(stages) + " |",
          "|---|---|---|---|---|" + "---|" * len(stages)]
    for kind, t in res["timing"].items():
        L.append(f"| {kind} | {t['documents']} | {t['mean_s']:.2f} s | {t['max_s']:.2f} s | {t['slowest']} | "
                 + " | ".join(f"{t['mean_stage_s'].get(k, 0):.2f}" for k in stages) + " |")
    if res["timing_all"]:
        t = res["timing_all"]
        L.append(f"| **all** | {t['documents']} | {t['mean_s']:.2f} s | {t['max_s']:.2f} s |"
                 + " |" * (len(stages) + 1))
    L.append("")

    L += ["## Outcomes", "", "| Status | Documents |", "|---|---|"]
    for k, n in sorted(o["status_counts"].items()):
        L.append(f"| {k} | {n} |")
    L.append("")
    if o["failure"]["files"]:
        L += ["Documents that failed although they should have worked:", ""]
        L += [f"- `{x['file']}`: {x['status']}: {x['error']}" for x in o["failure"]["files"]]
        L.append("")
    if o["expected_failures_not_failed"]:
        L += ["Broken files that were NOT rejected or failed (the system accepted them): "
              + ", ".join(f"`{x}`" for x in o["expected_failures_not_failed"]), ""]

    L += ["## Validation", "",
          f"Scored per document as a set of findings (`RULE`, or `MISSING_FIELD:field`). Expected findings = "
          f"`MISSING_FIELD` for every field of the rule's six (number, date, supplier, total, customer, currency) that the ground truth says is absent, plus the codes listed in "
          f"`expected_validation`. Not scored: {', '.join(v['ignored_rules'])} (the model's own confidence). "
          f"An 'unexpected' finding is often a correct alarm about an extraction mistake, so read it together "
          f"with the differences below.", "",
          f"- Precision {pct(v['precision']['value'])} ({v['precision']['tp']} of {v['precision']['reported']} findings expected)",
          f"- Recall {pct(v['recall']['value'])} ({v['recall']['tp']} of {v['recall']['expected']} expected findings reported)",
          f"- Planted errors caught: {frac(v['planted_errors_caught']['found'], v['planted_errors_caught']['of'])}",
          f"- Documents with nothing to report that still got a finding: "
          f"{frac(v['clean_documents_with_false_alarms']['count'], v['clean_documents_with_false_alarms']['of'])}", ""]
    if v["documents_with_differences"]:
        L += ["| Document | Expected but not reported | Reported but not expected |", "|---|---|---|"]
        for f, d in v["documents_with_differences"].items():
            L.append(f"| {f} | {', '.join(d['missed']) or '-'} | {', '.join(d['unexpected']) or '-'} |")
        L.append("")

    L += ["## Per document", "",
          "| Document | Type | Status | Time | OCR conf. | Fields right | Findings |", "|---|---|---|---|---|---|---|"]
    for r in records:
        pdoc = res["per_document"].get(r["file"])
        right = f"{pdoc['fields_correct']}/{pdoc['fields_total']}" if pdoc else "-"
        t = f"{r['seconds']:.2f} s" if r["seconds"] is not None else "-"
        conf = f"{r['ocr_confidence_mean']}" if r["ocr_confidence_mean"] is not None else "-"
        codes = ", ".join(sorted({f["code"] for f in r["findings"]})) or "-"
        L.append(f"| {r['file']} | {r['kind'] or r['file_type']} | {r['status']} | {t} | {conf} | {right} | {codes} |")
    L.append("")

    wrong = [(f, d) for f, d in res["per_document"].items() if d["differences"]]
    L += ["## Every difference from the ground truth", ""]
    if not wrong:
        L += ["None: every extracted field and line item matched.", ""]
    else:
        L += ["| Document | Invoice | Field | Kind | Truth | System output |", "|---|---|---|---|---|---|"]
        for f, d in wrong:
            for x in d["differences"]:
                L.append(f"| {f} | {x['invoice']} | {x['field']} | {x['kind']} | `{x['truth']}` | `{x['output']}` |")
        L.append("")
    failed = [r for r in records if r["status"] in ("failed", "rejected") and r["error_message"]]
    if failed:
        L += ["## Error messages of failed or rejected files", ""]
        L += [f"- `{r['file']}` ({r['status']}): {r['error_message']}" for r in failed]
        L.append("")

    L += ["## How the numbers are defined", "",
          "- **Documents that produced a result**: status `completed` or `needs_review`, among documents not marked "
          "`expected_failure`. A broken file that is rejected at upload or ends `failed` counts as handled "
          "correctly, not as a failure.",
          "- **Invoice accuracy**: every one of the 8 fields right, where a field the document does not state must "
          "be null. Extracted invoices are paired with labelled ones by invoice number, then by position.",
          "- **Line item precision / recall**: an extracted item is right only if description, quantity, unit price "
          "and line total all match. Precision = right / extracted, recall = right / labelled.",
          "- **Null rate**: of the fields the document states, the share the system left null (it declined to "
          "answer). **Hallucination rate**: of the fields the document does not state, the share it filled in. "
          "A wrong value for a field that does state something is counted under 'Wrong', and is in the "
          "'wrong or invented' share.",
          "- A rate is `n/a` when there is nothing to divide by (for example no labelled null fields), never 0%.",
          "- Time is measured around the pipeline call only (not upload or Redis); a real deployment adds queue "
          "wait time. Documents run one at a time, so these are not throughput figures.", ""]
    return "\n".join(L)


# ---- main ---------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", default=os.path.join(HERE, "labeled_dataset"))
    ap.add_argument("--out", default=HERE, help="folder for report.md and results.json")
    ap.add_argument("--reset", action="store_true", help="delete all rows of `documents` in the evaluation DB first")
    ap.add_argument("--limit", type=int, help="only the first N documents (by file name): cheap trial run")
    args = ap.parse_args()

    url = os.environ.get("EVAL_DATABASE_URL")
    if not url:
        print("Set EVAL_DATABASE_URL to a throw-away, migrated database (this script never uses DATABASE_URL).",
              file=sys.stderr)
        return 2
    try:
        truth = load_truth(args.dataset)
    except TruthError as exc:
        print(exc, file=sys.stderr)
        return 2

    upload_dir = tempfile.mkdtemp(prefix="docintel-eval-")
    os.environ.update(DATABASE_URL=url, UPLOAD_DIR=upload_dir, TASK_BACKEND="inline", ENVIRONMENT="development")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    from sqlalchemy import func, select, text

    from app.core import db
    from app.core.config import settings
    from app.models import Document

    try:
        with db.SessionLocal() as s:
            s.execute(text("select 1 from documents limit 1"))
            existing = s.scalar(select(func.count()).select_from(Document))
            if existing and not args.reset:
                print(f"The evaluation database already holds {existing} documents. Use an empty one or pass "
                      f"--reset to delete them.", file=sys.stderr)
                return 2
            if existing:
                s.execute(text("delete from documents"))
                s.commit()
    except Exception as exc:
        print(f"Cannot use the evaluation database ({type(exc).__name__}: {exc}). Is it migrated "
              f"(alembic upgrade head)?", file=sys.stderr)
        return 2

    names = sorted(truth["documents"])
    if args.limit:
        names = names[:args.limit]
    print(f"LLM provider: {settings.llm_provider}"
          f"{' (rule-based stand-in, not an AI model)' if settings.llm_provider == 'demo' else ' / ' + settings.llm_model}"
          f"; {len(names)} documents")
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    try:
        records = run_documents(args.dataset, truth, names, upload_dir)
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)
    wall = time.perf_counter() - t0

    truth_subset = {"documents": {n: truth["documents"][n] for n in names}}
    res = score(records, truth_subset)
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
                                    text=True).stdout.strip())
        commit += " (+ uncommitted changes)" if dirty else ""
    except Exception:
        commit = "unknown"
    meta = {"started": started.strftime("%Y-%m-%d %H:%M UTC"), "commit": commit, "documents": len(names),
            "dataset": os.path.relpath(args.dataset, ROOT), "llm_provider": settings.llm_provider,
            "llm_model": "demo-rules (regex stand-in)" if settings.llm_provider == "demo" else settings.llm_model,
            "ocr_languages": settings.ocr_languages, "embedding_provider": settings.embedding_provider,
            "wall_seconds": wall, "synthetic": "SAMPLE synthetic" in (truth.get("description") or "")}
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "results": res, "documents": records}, f, indent=2, ensure_ascii=False, default=str)
        f.write("\n")
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(render_report(meta, res, records) + "\n")
    print(f"Wrote {os.path.join(args.out, 'report.md')} and results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

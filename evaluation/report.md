# Evaluation report

Run at 2026-11-25 06:34 UTC on commit `19e30e6 (+ uncommitted changes)`. Dataset: **11 documents** (`evaluation/labeled_dataset`). LLM provider: **demo**, model: **demo-rules (regex stand-in)**. OCR languages: eng. Embeddings: hash. Total run time 2.0 s.

> **Warning: this run used the `demo` extractor, which is a regex stand-in and NOT an AI model.** These numbers describe the pipeline around it (text extraction, OCR, verification, validation, timing) and say nothing about how an LLM would score. Re-run with `LLM_PROVIDER=anthropic` and a key for real extraction numbers.

> **Small sample: 11 documents.** A rate over so few documents moves by several points when a single document changes. Treat these as a smoke test, not a benchmark (the target is 20-30 labelled documents).

> The dataset is the **synthetic sample** produced by `evaluation/make_sample_dataset.py` (clean, computer-generated pages). Real scans and real layouts are harder; do not read these numbers as expected real-world accuracy.

## Summary

| Measure | Result |
|---|---|
| Documents that produced a result | 100.0% (10/10) |
| Documents that failed (not expected to) | 0.0% (0/10) |
| Broken files that were rejected or failed cleanly | 1/1 |
| Produced a result but sent to review | 30.0% (3/10) |
| Invoice accuracy (all 8 fields right) | 100.0% (14/14) |
| Invoice accuracy incl. line items | 100.0% (14/14) |
| Line items: precision / recall | 100.0% / 100.0% |
| Null rate (field has a value, system said null) | 0.0% (0/86) |
| Hallucination rate (field has no value, system invented one) | 0.0% (0/26) |
| Wrong or invented share of the values it gave | 0.0% (0/86) |
| Validation precision / recall | 100.0% / 100.0% |
| Planted errors caught by validation | 100.0% (2/2) |

Extraction metrics cover the 10 documents that produced a result. Documents that failed are counted in the success/failure rows only.

## Field accuracy

Accuracy = correct / fields the document really states. Money within 0.01; text ignores case and extra spaces; dates must be the same day.

| Field | Accuracy | Correct | Wrong | Missed (null) | Invented (truth is null) | Correctly null |
|---|---|---|---|---|---|---|
| invoice_number | 100.0% (14/14) | 14 | 0 | 0 | 0 | 0 |
| invoice_date | 100.0% (13/13) | 13 | 0 | 0 | 0 | 1 |
| supplier_name | 100.0% (14/14) | 14 | 0 | 0 | 0 | 0 |
| customer_name | 100.0% (7/7) | 7 | 0 | 0 | 0 | 7 |
| subtotal | 100.0% (8/8) | 8 | 0 | 0 | 0 | 6 |
| tax | 100.0% (8/8) | 8 | 0 | 0 | 0 | 6 |
| total | 100.0% (14/14) | 14 | 0 | 0 | 0 | 0 |
| currency | 100.0% (8/8) | 8 | 0 | 0 | 0 | 6 |

Invoices: 14 labelled, 14 extracted, 14 paired with a labelled one.
Line items: 14 labelled, 14 extracted, 14 matched exactly (description, quantity, unit price, line total). Precision 100.0%, recall 100.0%.

## Processing time

Wall-clock seconds per document, from the start of the pipeline to the end of the embedding stage (one document at a time, one process), for documents that produced a result. Stage columns are mean seconds. There is no warm-up run: the first document processed also pays one-time costs (imports, connections).

| File type | Docs | Mean | Max | Slowest | embed | extract_fields | extract_text | save_results | segment | validate | validate_file |
|---|---|---|---|---|---|---|---|---|---|---|---|
| csv | 1 | 0.09 s | 0.09 s | sheet_german_semicolon.csv | 0.00 | 0.00 | 0.06 | 0.00 | 0.00 | 0.01 | 0.00 |
| jpeg (OCR) | 1 | 0.46 s | 0.46 s | scan_photo.jpg | 0.00 | 0.00 | 0.43 | 0.00 | 0.00 | 0.01 | 0.00 |
| pdf | 6 | 0.03 s | 0.03 s | inv_clean_01.pdf | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| pdf (OCR) | 1 | 0.94 s | 0.94 s | scan_image_only.pdf | 0.00 | 0.00 | 0.92 | 0.00 | 0.00 | 0.01 | 0.00 |
| xlsx | 1 | 0.03 s | 0.03 s | sheet_two_invoices.xlsx | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.01 | 0.00 |
| **all** | 10 | 0.17 s | 0.94 s | | | | | | | | |

## Outcomes

| Status | Documents |
|---|---|
| completed | 7 |
| failed | 1 |
| needs_review | 3 |

## Validation

Scored per document as a set of findings (`RULE`, or `MISSING_FIELD:field`). Expected findings = `MISSING_FIELD` for every field of the rule's six (number, date, supplier, total, customer, currency) that the ground truth says is absent, plus the codes listed in `expected_validation`. Not scored: LOW_CONFIDENCE (the model's own confidence). An 'unexpected' finding is often a correct alarm about an extraction mistake, so read it together with the differences below.

- Precision 100.0% (8 of 8 findings expected)
- Recall 100.0% (8 of 8 expected findings reported)
- Planted errors caught: 100.0% (2/2)
- Documents with nothing to report that still got a finding: 0.0% (0/5)

## Per document

| Document | Type | Status | Time | OCR conf. | Fields right | Findings |
|---|---|---|---|---|---|---|
| corrupt.pdf | pdf | failed | 0.25 s | - | - | - |
| inv_clean_01.pdf | pdf | completed | 0.03 s | - | 8/8 | - |
| inv_clean_02.pdf | pdf | completed | 0.02 s | - | 8/8 | - |
| inv_clean_03.pdf | pdf | completed | 0.02 s | - | 8/8 | - |
| inv_no_date_no_customer.pdf | pdf | needs_review | 0.03 s | - | 8/8 | MISSING_FIELD |
| inv_wrong_total.pdf | pdf | needs_review | 0.03 s | - | 8/8 | TOTAL_MISMATCH |
| inv_zz_duplicate_of_01.pdf | pdf | needs_review | 0.02 s | - | 8/8 | DUPLICATE_INVOICE |
| scan_image_only.pdf | pdf (OCR) | completed | 0.94 s | 94.8 | 8/8 | - |
| scan_photo.jpg | jpeg (OCR) | completed | 0.46 s | 94.1 | 8/8 | - |
| sheet_german_semicolon.csv | csv | completed | 0.09 s | - | 32/32 | MISSING_FIELD |
| sheet_two_invoices.xlsx | xlsx | completed | 0.03 s | - | 16/16 | MISSING_FIELD |

## Every difference from the ground truth

None: every extracted field and line item matched.

## Error messages of failed or rejected files

- `corrupt.pdf` (failed): The PDF has no pages.

## How the numbers are defined

- **Documents that produced a result**: status `completed` or `needs_review`, among documents not marked `expected_failure`. A broken file that is rejected at upload or ends `failed` counts as handled correctly, not as a failure.
- **Invoice accuracy**: every one of the 8 fields right, where a field the document does not state must be null. Extracted invoices are paired with labelled ones by invoice number, then by position.
- **Line item precision / recall**: an extracted item is right only if description, quantity, unit price and line total all match. Precision = right / extracted, recall = right / labelled.
- **Null rate**: of the fields the document states, the share the system left null (it declined to answer). **Hallucination rate**: of the fields the document does not state, the share it filled in. A wrong value for a field that does state something is counted under 'Wrong', and is in the 'wrong or invented' share.
- A rate is `n/a` when there is nothing to divide by (for example no labelled null fields), never 0%.
- Time is measured around the pipeline call only (not upload or Redis); a real deployment adds queue wait time. Documents run one at a time, so these are not throughput figures.


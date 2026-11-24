# Validation

Code: `backend/app/services/validation_service.py`, entry point `validate_document(session, document_id)`. Runs after extraction. All money maths uses `Decimal`, never float. Tolerance, limits and thresholds are settings (`VALIDATION_TOLERANCE=0.02`, `VALIDATION_MAX_AMOUNT=100000000`, `VALIDATION_MIN_CONFIDENCE=0.7`).

| rule_code | Function | Severity | Fires when |
|---|---|---|---|
| `MISSING_FIELD` | `rule_missing_field` | error / warning | `invoice_number`, `invoice_date`, `supplier_name`, `total` empty: error. `customer_name`, `currency` empty: warning. |
| `INVALID_NUMBER` | `rule_invalid_number` | error | subtotal, tax, total, or a line's quantity / unit price / line total is negative, above the max amount, or not a finite number (NaN, Infinity). |
| `LINE_ITEM_MISMATCH` | `rule_line_item_mismatch` | error | `quantity x unit_price` differs from `line_total` by more than 0.02. Skipped if any of the three is null. |
| `SUBTOTAL_MISMATCH` | `rule_subtotal_mismatch` | error | sum of line totals differs from `subtotal` by more than 0.02. Skipped unless every line has a total. |
| `TOTAL_MISMATCH` | `rule_total_mismatch` | error | `subtotal + tax` differs from `total` by more than 0.02. Skipped if any of the three is null. |
| `DUPLICATE_INVOICE` | `rule_duplicate_invoice` | error | same supplier (case/punctuation-insensitive) and invoice number already exist in an earlier upload, or earlier in the same document. |
| `DUPLICATE_FILE` | `rule_duplicate_file` | error | another document has the same `file_hash`. |
| `LOW_CONFIDENCE` | `rule_low_confidence` | warning | a non-null field or line item has confidence below 0.7. |
| `INVALID_DATE` | `rule_invalid_date` | warning | the date is in the future, or the extractor read a date text that could not be parsed. |
| `CROSS_DOCUMENT` | `rule_cross_document` | error | the same invoice number appears in another PDF/image or spreadsheet and the total (±0.02) or supplier differs. |

A finding is one `validation_errors` row (`rule_code`, `severity`, `field`, `message`, `result_id`; document-level findings like `DUPLICATE_FILE` have no result).

## Final status

| Unresolved findings on the latest extraction | Document |
|---|---|
| any error (including the extraction stage's `EXTRACT_*` errors) | `needs_review` |
| warnings only | `completed`, `is_flagged = true` |
| none | `completed`, `is_flagged = false` |

Re-running validation deletes its own unresolved findings and recomputes them. A finding a reviewer marked `resolved` is not created again. Only the latest `version` of a document's extraction is validated.

## Decisions to know about

- **PDF + spreadsheet copies of one invoice are not duplicates.** They are exactly what `CROSS_DOCUMENT` verifies, so `DUPLICATE_INVOICE` ignores pairs of different kinds (PDF/image vs spreadsheet). Two PDFs, or two spreadsheets, with the same supplier and number are duplicates. Spreadsheet-spreadsheet pairs are not cross-checked.
- **Only the later upload is flagged** for duplicates and cross-document mismatches (ordering by upload time), so two documents don't flag each other. Re-validate the earlier one after correcting the later one if needed.
- `CROSS_DOCUMENT` matches on invoice number alone, so two different suppliers that happen to use the same number (`INV-001`) will be reported as a supplier mismatch.
- `DUPLICATE_FILE` cannot fire today: `documents.file_hash` has a unique index, so the database refuses a second copy before validation runs. The rule is there for if that constraint is relaxed (tested by dropping the index inside a rolled-back transaction).
- Sum checks use the flat 0.02 tolerance. Invoices with many rounded lines, discounts, shipping or rounding lines can exceed it and need review.
- Negative amounts are errors as specified, so credit notes will go to review.

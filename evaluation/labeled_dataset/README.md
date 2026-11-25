# Labelled dataset

Put your 20-30 documents in this folder, next to `ground_truth.json`, and list the correct answer for each.
The files currently here are a **synthetic sample** (made by `evaluation/make_sample_dataset.py`) that shows the
format; delete them and write your own entries when you have real documents.

## Format of `ground_truth.json`

```json
{
  "format_version": 1,
  "description": "free text",
  "documents": {
    "acme_march.pdf": {
      "invoices": [
        {
          "invoice_number": "INV-2026-0042",
          "invoice_date": "2026-11-24",
          "supplier_name": "Acme Hosting Ltd",
          "customer_name": null,
          "subtotal": "721.00",
          "tax": "36.05",
          "total": "757.05",
          "currency": "USD",
          "line_items": [
            {"description": "Support hours", "quantity": "10", "unit_price": "45.00", "line_total": "450.00"}
          ]
        }
      ],
      "expected_validation": ["TOTAL_MISMATCH"],
      "expected_failure": false,
      "notes": "anything useful for you"
    }
  }
}
```

The key is the **file name** in this folder. A spreadsheet or a PDF holding several invoices has several entries
in `invoices`.

### Rules for the answers

* **Write what the document says, not what it should say.** If the printed total is wrong, the truth is the
  printed (wrong) total, and you list `TOTAL_MISMATCH` under `expected_validation`.
* **All 8 fields must be present in every invoice.** Use `null` when the document does not state it. This is
  not optional: `null` is how the evaluation knows the system must not invent a value (hallucination rate).
* Dates are `YYYY-MM-DD`. Money and quantities are strings or numbers with `.` as the decimal point, no
  thousands separators (`"1250.50"`). Currency is a 3-letter code.
* `line_items` may be empty. Every item has all four keys; use `null` for one the document lacks.
* `expected_validation`: rule codes the validator **should** report for this document that are not simply
  "a field is absent" (those are worked out for you from the `null`s): `TOTAL_MISMATCH`, `SUBTOTAL_MISMATCH`,
  `LINE_ITEM_MISMATCH`, `INVALID_NUMBER`, `INVALID_DATE`, `DUPLICATE_INVOICE`, `CROSS_DOCUMENT`.
  `LOW_CONFIDENCE` is never scored. Leave `[]` for a clean document.
* `expected_failure: true` for a file the system should refuse (corrupted, password-protected). Give it
  `"invoices": []`. Success means status `failed` (or rejected at upload), not a made-up result.
* Duplicates are judged in file-name order, so name the second copy so that it sorts after the first.
* Include some documents that are *missing* things (no date, no customer), some with arithmetic errors, some
  scans and photos, some spreadsheets: a set of only clean documents scores 100% and tells you nothing.

## Running

```bash
# an empty, migrated, throw-away database (never your real one)
EVAL_DATABASE_URL=postgresql://USER@HOST:PORT/DBNAME  python evaluation/run_evaluation.py --reset
```

`LLM_PROVIDER` / `LLM_API_KEY` / `LLM_MODEL` are read from your environment or `.env`, as in the app. With
`LLM_PROVIDER=anthropic` the run makes real API calls (one or more per invoice) and costs money. Use
`--limit 3` for a cheap trial. Output: `evaluation/report.md` and `evaluation/results.json`.

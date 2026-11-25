# Search

Code: `backend/app/services/search_service.py`, endpoint `GET /api/search` (`backend/app/api/search.py`).

```
GET /api/search?q=hosting renewal&mode=hybrid&supplier=acme&date_from=2026-01-01&date_to=2026-06-30&amount_min=100&status=completed&file_type=pdf&page=1&page_size=10
```

| Parameter | Meaning |
|---|---|
| `q` | what to look for (required, up to 500 characters) |
| `mode` | `keyword`, `semantic` or `hybrid` (default) |
| `supplier` | supplier name contains this text, case-insensitive (`%` and `_` are plain characters, not wildcards) |
| `invoice_number` | exact, case-insensitive |
| `date_from`, `date_to` | invoice date range, inclusive |
| `amount_min`, `amount_max` | range on the invoice total |
| `status`, `file_type` | document status; `pdf`, `xlsx`... |
| `page`, `page_size` | pagination (page size 1-50) |

Invoice-level filters (supplier, number, date, amount) look at the document's **latest extraction**; a document passes if at least one of its invoices matches all of them. A document with no extraction can still be found by text, but not when an invoice-level filter is set.

## Result

One result per document, best first:

```json
{"query": "hosting", "mode": "hybrid", "total": 4, "page": 1, "page_size": 10, "pages": 1, "truncated": false,
 "items": [{
   "document_id": "…", "filename": "digital_invoice.pdf", "status": "completed", "file_type": "pdf",
   "score": 0.0328, "matched_by": ["keyword", "semantic"], "keyword_rank": 1, "semantic_rank": 2,
   "snippet": {"text": "…Supplier: Acme Hosting Ltd…", "highlights": [[24, 31]], "page": 1},
   "invoice_count": 1,
   "invoice": {"invoice_number": "INV-2026-0042", "invoice_date": "2026-11-24", "supplier_name": "Acme Hosting Ltd",
               "customer_name": "Rahim Traders", "subtotal": "721.00", "tax": "36.05", "total": "757.05", "currency": "USD"}}]}
```

- `snippet` is **plain text plus highlight offsets**, never HTML. Page text comes from uploaded files, so the web page renders the text with React (which escapes it) and wraps the offsets in `<mark>`. Keyword snippets are an excerpt around the match (highlighted); semantic snippets are the best-matching chunk.
- `invoice` is the invoice that contains the matched page (the first one if none does); `invoice_count` says how many the document has.
- At most `SEARCH_MAX_RESULTS` (500) results are ranked; `truncated` says whether the cap was hit.

## How each mode works

**Keyword.** `document_pages.tsv` is a `tsvector` that PostgreSQL keeps up to date (a generated column) with a GIN index. The `simple` configuration means no stemming and no stop words: it works for any language but "invoice" will not match "invoices". `q` is read with `websearch_to_tsquery`, so `"exact phrase"`, `OR` and `-excluded` work and a malformed query never raises an error. Pages are ranked with `ts_rank_cd`; each document is represented by its best page.

**Semantic.** The query is embedded with the same provider as the chunks; the nearest chunks by cosine distance are found with the HNSW index (`hnsw.iterative_scan` is on, so filters do not starve the result), grouped by document (best chunk wins), and chunks below `SEARCH_SEMANTIC_MIN_SCORE` (0.05) are dropped. Needs `EMBEDDING_PROVIDER` other than `none`, otherwise 503 `search_unavailable`.

**Hybrid.** Runs both (top `SEARCH_CANDIDATES` = 100 documents each) and merges with **Reciprocal Rank Fusion**: `score = Σ 1 / (60 + rank)` over the rankings that contain the document. Only ranks are used, so no common scale between full-text rank and cosine similarity is needed; a document found by both rises above one found by a single method. Without an embedding provider hybrid quietly becomes keyword search.

## Embeddings (last pipeline stage)

Each page's text is cut into chunks of about **500 tokens with 10% overlap**, on word boundaries, one page at a time (a chunk never spans pages, so it knows its page number). Tokens are *estimated* as 4 characters each (`CHARS_PER_TOKEN`), not counted with the model's tokenizer; for Bengali and other scripts a chunk will hold more tokens than 500. The stage replaces the document's embeddings, so re-running it creates no duplicates; if it fails the document keeps its status.

The provider sits behind `EmbeddingClient` (`embed(texts) -> vectors`). Today only `hash` exists: a hashing embedder that captures **shared words, not meaning**, so semantic and hybrid search behave like fuzzy keyword search until a real model is plugged in. Vector length must equal `EMBEDDING_DIMENSION` (changing it needs a migration and re-embedding).

## Limits

- Supplier search is `ILIKE '%…%'`: fine for thousands of invoices, needs a trigram index (`pg_trgm`) beyond that.
- `simple` full-text search has no stemming or synonyms; the semantic side is what should cover that, once a real embedding model is in place.
- Search sees every document (there are no accounts or tenants yet).

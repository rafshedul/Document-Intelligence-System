"""Search over documents: keyword (PostgreSQL full-text), semantic (pgvector) and hybrid (RRF).

Every mode returns one result per *document*, ranked, with the best matching snippet and page.
Filters apply to the document's latest extraction (supplier, invoice number, date, amount) and to the
document itself (status, file type).

Snippets are returned as plain text plus highlight offsets, never as HTML: page text comes from
uploaded files and must not be trusted as markup.
"""
import math
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, exists, func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Document, DocumentPage, Embedding, ExtractedResult
from app.services.embedding_service import EmbeddingClient, get_embedding_client

FTS_CONFIG = "simple"  # must match the generated column document_pages.tsv
MARK_START, MARK_END = "", ""  # private-use characters that cannot occur in normal text
SNIPPET_CHARS = 300
MODES = ("keyword", "semantic", "hybrid")


class SearchUnavailable(Exception):
    """Semantic search needs an embedding provider and there is none."""


@dataclass
class SearchFilters:
    supplier: str | None = None  # contains, case-insensitive
    invoice_number: str | None = None  # exact, case-insensitive
    date_from: date | None = None
    date_to: date | None = None
    amount_min: Decimal | None = None  # on the invoice total
    amount_max: Decimal | None = None
    status: str | None = None
    file_type: str | None = None

    def invoice_level(self) -> bool:
        return any(v is not None for v in (self.supplier, self.invoice_number, self.date_from, self.date_to,
                                           self.amount_min, self.amount_max))


@dataclass
class Snippet:
    text: str
    highlights: list[tuple[int, int]] = field(default_factory=list)  # [start, end) offsets into text
    page: int | None = None


@dataclass
class Ranked:
    document_id: object
    score: float
    keyword_rank: int | None = None
    semantic_rank: int | None = None
    keyword_page: int | None = None  # best page for the keyword match
    semantic_chunk: tuple[int | None, str] | None = None  # (page, chunk text) of the best chunk
    snippet: Snippet | None = None

    @property
    def matched_by(self) -> list[str]:
        return [m for m, r in (("keyword", self.keyword_rank), ("semantic", self.semantic_rank)) if r is not None]


@dataclass
class SearchPage:
    items: list[dict]
    total: int
    page: int
    page_size: int
    pages: int
    truncated: bool  # more matches exist than search_max_results


# ---- filters -----------------------------------------------------------------------------

def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def document_conditions(f: SearchFilters) -> list:
    """SQL conditions on `documents` for the filters. Invoice-level ones look at the latest version."""
    conds = []
    if f.status:
        conds.append(Document.status == f.status)
    if f.file_type:
        conds.append(Document.file_type == f.file_type.lower().lstrip("."))
    if f.invoice_level():
        latest = (select(func.max(ExtractedResult.version)).where(ExtractedResult.document_id == Document.id)
                  .correlate(Document).scalar_subquery())
        r = [ExtractedResult.document_id == Document.id, ExtractedResult.version == latest]
        if f.supplier:
            r.append(ExtractedResult.supplier_name.ilike(_like(f.supplier), escape="\\"))
        if f.invoice_number:
            r.append(func.lower(ExtractedResult.invoice_number) == f.invoice_number.strip().lower())
        if f.date_from:
            r.append(ExtractedResult.invoice_date >= f.date_from)
        if f.date_to:
            r.append(ExtractedResult.invoice_date <= f.date_to)
        if f.amount_min is not None:
            r.append(ExtractedResult.total >= f.amount_min)
        if f.amount_max is not None:
            r.append(ExtractedResult.total <= f.amount_max)
        conds.append(exists().where(and_(*r)))
    return conds


# ---- keyword ------------------------------------------------------------------------------

def keyword_search(session: Session, q: str, f: SearchFilters, limit: int) -> list[Ranked]:
    """PostgreSQL full-text search. `q` uses web-search syntax: words, "exact phrase", OR, -excluded."""
    tsq = func.websearch_to_tsquery(FTS_CONFIG, q)
    rank = func.ts_rank_cd(DocumentPage.tsv, tsq).label("rank")
    per_page = (select(DocumentPage.document_id, DocumentPage.page_number, rank)
                .join(Document, Document.id == DocumentPage.document_id)
                .where(DocumentPage.tsv.op("@@")(tsq), *document_conditions(f)).subquery())
    best = (select(per_page.c.document_id, per_page.c.page_number, per_page.c.rank)
            .distinct(per_page.c.document_id)  # one row per document: its best page
            .order_by(per_page.c.document_id, per_page.c.rank.desc(), per_page.c.page_number).subquery())
    rows = session.execute(select(best).order_by(best.c.rank.desc(), best.c.document_id).limit(limit)).all()
    return [Ranked(r.document_id, float(r.rank), keyword_rank=i, keyword_page=r.page_number)
            for i, r in enumerate(rows, start=1)]


def _headlines(session: Session, q: str, pairs: list[tuple]) -> dict[tuple, Snippet]:
    """Highlighted excerpts for (document_id, page_number) pairs."""
    if not pairs:
        return {}
    tsq = func.websearch_to_tsquery(FTS_CONFIG, q)
    head = func.ts_headline(FTS_CONFIG, DocumentPage.extracted_text, tsq,
                            f"StartSel={MARK_START}, StopSel={MARK_END}, MaxWords=35, MinWords=15, "
                            "MaxFragments=1, HighlightAll=false")
    rows = session.execute(select(DocumentPage.document_id, DocumentPage.page_number, head, DocumentPage.extracted_text)
                           .where(func.row(DocumentPage.document_id, DocumentPage.page_number).in_(
                               [func.row(d, p) for d, p in pairs]))).all()
    out = {}
    for d, p, h, full in rows:
        sn = _from_marked(h or "")
        if not sn.highlights and full:  # nothing marked (e.g. a stop-word-only match): show the start of the page
            sn = Snippet(_clip(full))
        sn.page = p
        out[(d, p)] = sn
    return out


def _from_marked(marked: str) -> Snippet:
    """Turn text with marker characters into plain text plus highlight offsets."""
    plain, spans, start = [], [], None
    pos = 0
    for ch in marked:
        if ch == MARK_START:
            start = pos
        elif ch == MARK_END:
            if start is not None:
                spans.append((start, pos))
            start = None
        else:
            plain.append(ch)
            pos += 1
    return Snippet(re.sub(r"\s+", " ", "".join(plain)).strip() if not spans else "".join(plain), spans)


def _clip(s: str, n: int = SNIPPET_CHARS) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


# ---- semantic ------------------------------------------------------------------------------

def semantic_search(session: Session, q: str, f: SearchFilters, limit: int,
                    client: EmbeddingClient | None = None) -> list[Ranked]:
    """Nearest chunks by cosine distance, grouped by document (best chunk wins)."""
    client = client or get_embedding_client()
    if client is None:
        raise SearchUnavailable("Semantic search is off: no embedding provider is configured (EMBEDDING_PROVIDER).")
    qvec = client.embed([q])[0]
    dist = Embedding.embedding.cosine_distance(qvec).label("dist")
    # With filters, HNSW may stop early; this makes it keep scanning until it has enough rows.
    session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
    rows = session.execute(
        select(Embedding.document_id, Embedding.page_number, Embedding.chunk_text, dist)
        .join(Document, Document.id == Embedding.document_id).where(*document_conditions(f))
        .order_by(dist).limit(max(limit * 5, 100))).all()
    best: dict = {}
    for r in sorted(rows, key=lambda r: r.dist):  # relaxed_order can return rows slightly out of order
        sim = 1.0 - float(r.dist)
        if sim >= settings.search_semantic_min_score and r.document_id not in best:
            best[r.document_id] = (sim, r.page_number, r.chunk_text)
    ordered = sorted(best.items(), key=lambda kv: -kv[1][0])[:limit]
    return [Ranked(d, sim, semantic_rank=i, semantic_chunk=(page, chunk))
            for i, (d, (sim, page, chunk)) in enumerate(ordered, start=1)]


# ---- hybrid ---------------------------------------------------------------------------------

def rrf(rank_lists: list[list], k: int | None = None) -> dict:
    """Reciprocal Rank Fusion: score(item) = sum over lists of 1 / (k + rank). Ranks start at 1.

    Only ranks are used, so scores from different systems (full-text rank, cosine similarity) need no
    common scale.
    """
    k = settings.search_rrf_k if k is None else k
    scores: dict = {}
    for ranking in rank_lists:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


def hybrid_search(session: Session, q: str, f: SearchFilters, limit: int,
                  client: EmbeddingClient | None = None) -> list[Ranked]:
    n = settings.search_candidates
    kw = keyword_search(session, q, f, n)
    try:
        sem = semantic_search(session, q, f, n, client)
    except SearchUnavailable:
        sem = []  # no embeddings configured: hybrid degrades to keyword only
    scores = rrf([[r.document_id for r in kw], [r.document_id for r in sem]])
    merged: dict = {}
    for r in kw:
        merged[r.document_id] = Ranked(r.document_id, 0.0, keyword_rank=r.keyword_rank, keyword_page=r.keyword_page)
    for r in sem:
        m = merged.setdefault(r.document_id, Ranked(r.document_id, 0.0))
        m.semantic_rank, m.semantic_chunk = r.semantic_rank, r.semantic_chunk
    for d, m in merged.items():
        m.score = scores[d]
    return sorted(merged.values(), key=lambda m: (-m.score, str(m.document_id)))[:limit]


# ---- entry point -------------------------------------------------------------------------------

def search(session: Session, q: str, mode: str = "hybrid", filters: SearchFilters | None = None, page: int = 1,
           page_size: int = 10, client: EmbeddingClient | None = None) -> SearchPage:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    filters = filters or SearchFilters()
    cap = settings.search_max_results
    if mode == "keyword":
        ranked = keyword_search(session, q, filters, cap)
    elif mode == "semantic":
        ranked = semantic_search(session, q, filters, cap, client)
    else:
        ranked = hybrid_search(session, q, filters, cap, client)
    total = len(ranked)
    window = ranked[(page - 1) * page_size: page * page_size]
    heads = _headlines(session, q, [(r.document_id, r.keyword_page) for r in window if r.keyword_page is not None]) \
        if mode != "semantic" else {}
    for r in window:  # keyword snippet when there is one, else the best semantic chunk
        if r.keyword_page is not None and (r.document_id, r.keyword_page) in heads:
            r.snippet = heads[(r.document_id, r.keyword_page)]
        elif r.semantic_chunk:
            r.snippet = Snippet(_clip(r.semantic_chunk[1]), [], r.semantic_chunk[0])
    return SearchPage(_attach_documents(session, window), total, page, page_size,
                      math.ceil(total / page_size) if total else 0, total >= cap)


def _attach_documents(session: Session, window: list[Ranked]) -> list[dict]:
    """Add filename, status and the main extracted fields to each result."""
    if not window:
        return []
    ids = [r.document_id for r in window]
    docs = {d.id: d for d in session.scalars(select(Document).where(Document.id.in_(ids)))}
    latest = (select(ExtractedResult.document_id, func.max(ExtractedResult.version).label("v"))
              .where(ExtractedResult.document_id.in_(ids)).group_by(ExtractedResult.document_id).subquery())
    invoices: dict = {}
    for er in session.scalars(select(ExtractedResult).join(latest, and_(
            latest.c.document_id == ExtractedResult.document_id, latest.c.v == ExtractedResult.version))
            .order_by(ExtractedResult.document_id, ExtractedResult.invoice_index)):
        invoices.setdefault(er.document_id, []).append(er)
    items = []
    for r in window:
        d = docs[r.document_id]
        mine = invoices.get(d.id, [])
        page = r.snippet.page if r.snippet else None
        pick = next((i for i in mine if page and i.page_start and i.page_end and i.page_start <= page <= i.page_end),
                    mine[0] if mine else None)  # the invoice that contains the matched page
        items.append({
            "document_id": str(d.id), "filename": d.original_filename, "status": d.status.value,
            "file_type": d.file_type, "score": round(r.score, 6), "matched_by": r.matched_by,
            "keyword_rank": r.keyword_rank, "semantic_rank": r.semantic_rank,
            "snippet": None if r.snippet is None else
            {"text": r.snippet.text, "highlights": [list(h) for h in r.snippet.highlights], "page": r.snippet.page},
            "invoice_count": len(mine),
            "invoice": None if pick is None else {
                "invoice_index": pick.invoice_index, "invoice_number": pick.invoice_number,
                "invoice_date": pick.invoice_date.isoformat() if pick.invoice_date else None,
                "supplier_name": pick.supplier_name, "customer_name": pick.customer_name,
                "subtotal": _s(pick.subtotal), "tax": _s(pick.tax), "total": _s(pick.total), "currency": pick.currency},
        })
    return items


def _s(v):
    return None if v is None else str(v)

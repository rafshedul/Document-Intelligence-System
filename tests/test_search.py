"""Keyword, semantic and hybrid search on a small known corpus. Needs TEST_DATABASE_URL."""
import hashlib
import os
import uuid
from datetime import date
from decimal import Decimal as D

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core import db
from app.core.config import settings
from app.main import create_app
from app.models import Document, DocumentPage, DocumentStatus, Embedding, ExtractedResult, ExtractionMethod
from app.services import embedding_service, search_service as svc
from app.services.search_service import SearchFilters, SearchUnavailable


# ---- pure functions (no database) ------------------------------------------------------------

def test_rrf_rewards_agreement_between_rankings():
    scores = svc.rrf([["a", "b", "c"], ["b", "d"]], k=60)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)  # ranked 2nd and 1st
    assert scores["a"] == pytest.approx(1 / 61) and scores["d"] == pytest.approx(1 / 62)
    assert max(scores, key=scores.get) == "b"  # in both lists beats #1 in only one
    assert svc.rrf([]) == {} and svc.rrf([[], []]) == {}


def test_rrf_k_flattens_the_difference_between_ranks():
    low, high = svc.rrf([["a", "b"]], k=1), svc.rrf([["a", "b"]], k=1000)
    assert low["a"] / low["b"] > high["a"] / high["b"]


def test_marked_text_becomes_plain_text_plus_offsets():
    s = svc._from_marked(f"the {svc.MARK_START}hosting{svc.MARK_END} renewal and more {svc.MARK_START}hosting{svc.MARK_END}")
    assert s.text == "the hosting renewal and more hosting"
    assert [s.text[a:b] for a, b in s.highlights] == ["hosting", "hosting"]
    assert svc._from_marked("no marks   here\n").text == "no marks here"


def test_like_escaping():
    assert svc._like("50%_off\\") == "%50\\%\\_off\\\\%"


# ---- the corpus -------------------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = sa.create_engine(url)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "embedding_provider", "hash")
    monkeypatch.setattr(settings, "search_semantic_min_score", 0.05)
    with Session(engine) as s:
        s.execute(sa.delete(Document)); s.commit()
    yield engine
    with Session(engine) as s:
        s.execute(sa.delete(Document)); s.commit()


def add(s, name, pages, supplier=None, number=None, on=None, total=None, status=DocumentStatus.completed,
        ftype="pdf", span=None):
    d = Document(original_filename=name, stored_path="/x", file_type=ftype, file_size=1, status=status,
                 file_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(), page_count=len(pages))
    s.add(d)
    s.flush()
    for i, t in enumerate(pages, 1):
        s.add(DocumentPage(document_id=d.id, page_number=i, extracted_text=t, extraction_method=ExtractionMethod.digital))
    if number or supplier:
        s.add(ExtractedResult(document_id=d.id, version=1, invoice_index=0, invoice_number=number, supplier_name=supplier,
                              invoice_date=on, total=total, currency="USD", page_start=1, page_end=len(pages)))
    s.commit()
    embedding_service.embed_document(s, d.id)
    return d


@pytest.fixture
def corpus(env):
    with Session(env) as s:
        docs = {
            "A": add(s, "a.pdf", ["annual web hosting renewal for the company website"], "Acme Hosting Ltd", "INV-A1",
                     date(2026, 1, 10), D("100.00")),
            "B": add(s, "b.xlsx", ["office furniture chairs and desks delivery to the warehouse"], "Globex Corporation",
                     "INV-B1", date(2026, 3, 15), D("5000.00"), DocumentStatus.needs_review, "xlsx"),
            "C": add(s, "c.pdf", ["domain renewal and technical support hours"], "Acme Hosting Ltd", "INV-C1",
                     date(2026, 5, 20), D("250.00")),
            "D": add(s, "d.pdf", ["hosting hosting hosting server hosting"], status=DocumentStatus.failed),
            "E": add(s, "e.pdf", ["cover page nothing here", "the warranty clause covers laptops and monitors",
                                  "closing remarks"], "Initech", "INV-E1", date(2026, 7, 1), D("999.99")),
        }
        ids = {k: d.id for k, d in docs.items()}
    return ids


def run(env, q, mode="keyword", page=1, page_size=10, client=None, **filters):
    with Session(env) as s:
        return svc.search(s, q, mode, SearchFilters(**filters), page, page_size, client)


def names(env, corpus, q, mode="keyword", **kw):
    inv = {v: k for k, v in corpus.items()}
    return [inv[uuid.UUID(i["document_id"])] for i in run(env, q, mode, **kw).items]


# ---- keyword -------------------------------------------------------------------------------------

def test_keyword_finds_documents_by_word_and_ranks_by_relevance(env, corpus):
    assert names(env, corpus, "hosting") == ["D", "A"]  # D repeats the word, A mentions it once
    assert set(names(env, corpus, "renewal")) == {"A", "C"}
    assert names(env, corpus, "HOSTING") == ["D", "A"]  # case does not matter
    assert names(env, corpus, "nonexistentword") == []


def test_keyword_result_shape_snippet_and_fields(env, corpus):
    r = run(env, "hosting").items
    a = next(i for i in r if i["document_id"] == str(corpus["A"]))
    assert {"document_id", "filename", "status", "file_type", "score", "matched_by", "snippet", "invoice"} <= set(a)
    assert a["filename"] == "a.pdf" and a["matched_by"] == ["keyword"] and a["score"] > 0 and a["keyword_rank"] == 2
    sn = a["snippet"]
    assert sn["page"] == 1 and "annual web hosting renewal" in sn["text"]
    assert [sn["text"][x:y] for x, y in sn["highlights"]] == ["hosting"]
    assert a["invoice"]["invoice_number"] == "INV-A1" and a["invoice"]["supplier_name"] == "Acme Hosting Ltd"
    assert a["invoice"]["total"] == "100.00" and a["invoice"]["invoice_date"] == "2026-01-10" and a["invoice_count"] == 1
    d = next(i for i in r if i["document_id"] == str(corpus["D"]))
    assert d["invoice"] is None and d["invoice_count"] == 0  # no extraction: still searchable by text


def test_keyword_reports_the_page_where_it_matched(env, corpus):
    hit = run(env, "warranty").items[0]
    assert hit["filename"] == "e.pdf" and hit["snippet"]["page"] == 2
    assert hit["invoice"]["invoice_number"] == "INV-E1"


def test_keyword_search_syntax(env, corpus):
    assert names(env, corpus, '"domain renewal"') == ["C"]  # exact phrase
    assert names(env, corpus, "renewal -domain") == ["A"]  # exclusion
    assert set(names(env, corpus, "furniture OR laptops")) == {"B", "E"}
    assert names(env, corpus, "furniture warranty") == []  # words must all be present


@pytest.mark.parametrize("q", ['"unbalanced', "'; DROP TABLE documents; --", "!!!", "()", "a" * 500, "চালান", "%_\\", "\x00x"])
def test_awkward_queries_never_crash(env, corpus, q):
    q = q.replace("\x00", "")
    run(env, q or "x")  # may find nothing, must not raise
    with Session(env) as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(Document)) == 5  # and nothing was dropped


def test_keyword_filters(env, corpus):
    assert set(names(env, corpus, "renewal", supplier="acme")) == {"A", "C"}
    assert names(env, corpus, "furniture", supplier="Glob") == ["B"]
    assert names(env, corpus, "furniture", supplier="acme") == []
    assert names(env, corpus, "hosting", invoice_number="inv-a1") == ["A"]  # exact, case-insensitive
    assert names(env, corpus, "hosting", invoice_number="INV-A") == []
    assert names(env, corpus, "renewal", date_from=date(2026, 3, 1), date_to=date(2026, 6, 30)) == ["C"]
    assert names(env, corpus, "renewal", date_from=date(2026, 5, 20)) == ["C"]  # boundaries are inclusive
    assert names(env, corpus, "renewal", date_to=date(2026, 1, 10)) == ["A"]
    assert names(env, corpus, "renewal", amount_min=D("200")) == ["C"]
    assert names(env, corpus, "renewal", amount_max=D("150")) == ["A"]
    assert names(env, corpus, "renewal", amount_min=D("100"), amount_max=D("100")) == ["A"]
    assert names(env, corpus, "hosting", status="failed") == ["D"]
    assert names(env, corpus, "hosting", status="needs_review") == []
    assert names(env, corpus, "furniture", file_type="xlsx") == ["B"]
    assert names(env, corpus, "furniture", file_type=".XLSX") == ["B"]
    assert names(env, corpus, "furniture", file_type="pdf") == []
    assert names(env, corpus, "renewal", supplier="acme", date_from=date(2026, 2, 1), amount_min=D("1")) == ["C"]


def test_filter_text_is_not_a_wildcard(env, corpus):
    assert names(env, corpus, "renewal", supplier="%") == []  # a literal percent sign, not "anything"
    assert names(env, corpus, "renewal", supplier="_cme") == []


def test_filters_use_only_the_latest_extraction(env, corpus):
    with Session(env) as s:
        s.add(ExtractedResult(document_id=corpus["A"], version=2, invoice_index=0, invoice_number="INV-A2",
                              supplier_name="Newco", total=D("1.00")))
        s.commit()
    assert names(env, corpus, "hosting", supplier="Newco") == ["A"]
    assert names(env, corpus, "hosting", supplier="Acme") == []  # version 1 no longer counts
    assert run(env, "hosting", supplier="Newco").items[0]["invoice"]["invoice_number"] == "INV-A2"


def test_the_invoice_containing_the_matched_page_is_returned(env, corpus):
    with Session(env) as s:
        d = add(s, "multi.pdf", ["intro", "alpha invoice page", "beta invoice page with needle"], "S", "M-1")
        r = s.scalars(sa.select(ExtractedResult).where(ExtractedResult.document_id == d.id)).one()
        r.page_start, r.page_end = 1, 2
        s.add(ExtractedResult(document_id=d.id, version=1, invoice_index=1, invoice_number="M-2", supplier_name="S",
                              page_start=3, page_end=3))
        s.commit()
    hit = run(env, "needle").items[0]
    assert hit["invoice"]["invoice_number"] == "M-2" and hit["invoice_count"] == 2 and hit["snippet"]["page"] == 3


def test_snippets_are_plain_text_with_offsets_never_markup(env):
    with Session(env) as s:
        add(s, "x.pdf", ["<script>alert(1)</script> hosting <b>bold</b> notes"])
    hit = run(env, "hosting").items[0]["snippet"]
    assert [hit["text"][a:b] for a, b in hit["highlights"]] == ["hosting"]  # highlights are offsets, not <mark> tags
    assert svc.MARK_START not in hit["text"] and svc.MARK_END not in hit["text"]
    assert "<mark" not in hit["text"] and "<em" not in hit["text"]
    # a semantic snippet is the raw chunk, so the page must escape it (React does; the API never sends HTML itself)
    raw = run(env, "alert bold notes hosting", "semantic").items[0]["snippet"]["text"]
    assert "<script>alert(1)</script>" in raw


# ---- semantic ---------------------------------------------------------------------------------------

def test_semantic_finds_the_closest_chunk(env, corpus):
    items = run(env, "office chairs desks", "semantic").items
    assert items[0]["filename"] == "b.xlsx" and items[0]["matched_by"] == ["semantic"]
    assert items[0]["semantic_rank"] == 1 and items[0]["keyword_rank"] is None and 0 < items[0]["score"] <= 1
    assert items[0]["snippet"]["text"].startswith("office furniture") and items[0]["snippet"]["highlights"] == []
    scores = [i["score"] for i in items]
    assert scores == sorted(scores, reverse=True)


def test_semantic_reports_the_page_of_the_chunk(env, corpus):
    hit = run(env, "warranty covers laptops monitors", "semantic").items[0]
    assert hit["filename"] == "e.pdf" and hit["snippet"]["page"] == 2


def test_semantic_returns_each_document_once_even_with_many_chunks(env):
    with Session(env) as s:
        add(s, "long.pdf", ["payment terms net thirty days " * 400])
        assert s.scalar(sa.select(sa.func.count()).select_from(Embedding)) > 3
    items = run(env, "payment terms", "semantic").items
    assert [i["filename"] for i in items] == ["long.pdf"]


def test_semantic_ignores_weak_matches(env, corpus, monkeypatch):
    assert run(env, "zzzz qqqq", "semantic").items == []  # nothing in common
    exact = "office furniture chairs and desks delivery to the warehouse"
    assert names(env, corpus, exact, "semantic")[0] == "B"  # identical text: similarity 1.0 (others share only "and", "the")
    monkeypatch.setattr(settings, "search_semantic_min_score", 0.999)
    assert names(env, corpus, exact, "semantic") == ["B"]
    assert names(env, corpus, "office furniture", "semantic") == []  # only part of it: below the bar


def test_semantic_uses_the_same_filters(env, corpus):
    assert names(env, corpus, "office chairs desks", "semantic", supplier="acme") == []
    assert names(env, corpus, "office chairs desks", "semantic", status="needs_review") == ["B"]
    assert names(env, corpus, "office chairs desks", "semantic", file_type="pdf") == []
    assert names(env, corpus, "office chairs desks", "semantic", amount_min=D("4000")) == ["B"]


def test_semantic_search_needs_an_embedding_provider(env, corpus, monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "none")
    with pytest.raises(SearchUnavailable):
        run(env, "office", "semantic")
    assert names(env, corpus, "furniture") == ["B"]  # keyword search does not need it


# ---- hybrid -----------------------------------------------------------------------------------------

def test_hybrid_puts_documents_found_by_both_methods_first(env, corpus):
    items = run(env, "hosting renewal", "hybrid").items
    top = items[0]
    assert top["filename"] == "a.pdf" and top["matched_by"] == ["keyword", "semantic"]
    assert top["score"] == pytest.approx(1 / (60 + top["keyword_rank"]) + 1 / (60 + top["semantic_rank"]), abs=1e-6)
    assert top["snippet"]["highlights"]  # the highlighted keyword excerpt wins over the plain chunk
    assert [i["score"] for i in items] == sorted((i["score"] for i in items), reverse=True)
    assert len({i["document_id"] for i in items}) == len(items)  # no document twice


def test_hybrid_includes_semantic_only_matches(env, corpus):
    # "chairs" never appears in D; "desks furniture" shares no word with the keyword query below
    items = run(env, "furniture", "hybrid").items
    assert items[0]["filename"] == "b.xlsx" and items[0]["matched_by"] == ["keyword", "semantic"]
    only_sem = [i for i in run(env, "warehouse delivery", "hybrid").items if i["keyword_rank"] is not None]
    assert only_sem and all("semantic" in i["matched_by"] or "keyword" in i["matched_by"] for i in only_sem)


def test_hybrid_without_embeddings_falls_back_to_keyword(env, corpus, monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "none")
    items = run(env, "hosting", "hybrid").items
    assert [i["filename"] for i in items] == ["d.pdf", "a.pdf"] and all(i["matched_by"] == ["keyword"] for i in items)


def test_hybrid_applies_filters_to_both_sides(env, corpus):
    assert names(env, corpus, "hosting renewal", "hybrid", supplier="acme") and \
        set(names(env, corpus, "hosting renewal", "hybrid", supplier="acme")) <= {"A", "C"}
    assert names(env, corpus, "hosting renewal", "hybrid", supplier="globex") == []


# ---- pagination ------------------------------------------------------------------------------------------

def test_pagination(env, corpus):
    p1 = run(env, "the", page=1, page_size=2)
    p2 = run(env, "the", page=2, page_size=2)
    p9 = run(env, "the", page=9, page_size=2)
    assert p1.total == p2.total >= 3 and p1.pages == -(-p1.total // 2) and (p1.page, p2.page, p1.page_size) == (1, 2, 2)
    assert len(p1.items) == 2 and p2.items and not {i["document_id"] for i in p1.items} & {i["document_id"] for i in p2.items}
    assert p9.items == [] and p9.total == p1.total
    assert run(env, "nothingmatches").pages == 0


def test_results_are_capped(env, corpus, monkeypatch):
    monkeypatch.setattr(settings, "search_max_results", 2)
    r = run(env, "the")
    assert r.total == 2 and r.truncated is True
    assert run(env, "furniture").truncated is False


# ---- chunking and the pipeline stage ------------------------------------------------------------------------

def test_chunks_are_about_500_tokens_with_10_percent_overlap(env):
    words = " ".join(f"w{i:04d}" for i in range(2000))
    chunks = embedding_service.chunk_text(words)
    size, overlap = embedding_service.chunk_size_chars()
    assert (size, overlap) == (2000, 200)
    assert all(len(c) <= size for c in chunks) and len(chunks) >= 7
    a, b = chunks[0], chunks[1]
    shared = next(k for k in range(len(a), 0, -1) if b.startswith(a[-k:]))
    assert 150 <= shared <= 215  # the next chunk repeats about the last 10%


def test_embeddings_know_their_page(env):
    with Session(env) as s:
        d = add(s, "p.pdf", ["first page words", "", "third page words"])
        pages = [e.page_number for e in s.scalars(sa.select(Embedding).where(Embedding.document_id == d.id)
                                                  .order_by(Embedding.chunk_index))]
    assert pages == [1, 3]


# ---- the endpoint ------------------------------------------------------------------------------------------------

@pytest.fixture
def client(env):
    return TestClient(create_app())


def test_endpoint_returns_results(client, corpus):
    r = client.get("/api/search", params={"q": "renewal", "mode": "keyword", "supplier": "acme", "date_from": "2026-02-01"})
    assert r.status_code == 200
    body = r.json()
    assert (body["query"], body["mode"], body["total"], body["page"], body["pages"]) == ("renewal", "keyword", 1, 1, 1)
    hit = body["items"][0]
    assert hit["filename"] == "c.pdf" and hit["invoice"]["total"] == "250.00" and hit["snippet"]["highlights"]


def test_endpoint_defaults_to_hybrid_and_paginates(client, corpus):
    body = client.get("/api/search", params={"q": "the", "page_size": 2}).json()
    assert body["mode"] == "hybrid" and len(body["items"]) == 2 and body["pages"] >= 2
    assert client.get("/api/search", params={"q": "the", "page_size": 2, "page": 2}).json()["page"] == 2


@pytest.mark.parametrize("params", [
    {}, {"q": ""}, {"q": "   "}, {"q": "x", "mode": "fuzzy"}, {"q": "x", "page": 0}, {"q": "x", "page_size": 0},
    {"q": "x", "page_size": 51}, {"q": "x", "status": "nope"}, {"q": "x", "date_from": "2026-13-01"},
    {"q": "x", "date_from": "2026-05-01", "date_to": "2026-01-01"}, {"q": "x", "amount_min": "10", "amount_max": "5"},
    {"q": "x", "amount_min": "abc"}, {"q": "y" * 501},
])
def test_endpoint_rejects_bad_parameters(client, params):
    r = client.get("/api/search", params=params)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_endpoint_semantic_without_provider_is_503(client, corpus, monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "none")
    r = client.get("/api/search", params={"q": "office", "mode": "semantic"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "search_unavailable"
    assert client.get("/api/search", params={"q": "furniture", "mode": "hybrid"}).status_code == 200


def test_endpoint_is_in_the_openapi_docs(client):
    op = client.get("/openapi.json").json()["paths"]["/api/search"]["get"]
    assert {p["name"] for p in op["parameters"]} >= {"q", "mode", "supplier", "invoice_number", "date_from", "date_to",
                                                      "amount_min", "amount_max", "status", "file_type", "page", "page_size"}

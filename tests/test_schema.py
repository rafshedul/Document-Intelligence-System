"""Schema tests. Metadata tests run anywhere; DB tests need TEST_DATABASE_URL
pointing at a migrated (alembic upgrade head) Postgres with pgvector."""
import os
import sys
import uuid
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import Base, Document, DocumentStatus, Embedding, ExtractedResult, LineItem  # noqa: E402


def test_every_foreign_key_is_indexed():
    for table in Base.metadata.tables.values():
        covered = {idx.columns.keys()[0] for idx in table.indexes}
        covered |= {
            c.columns.keys()[0]
            for c in table.constraints
            if isinstance(c, sa.UniqueConstraint)
        }
        for fk in table.foreign_keys:
            assert fk.parent.name in covered, f"{table.name}.{fk.parent.name} has no index"


def test_no_float_columns_and_money_is_numeric_14_2():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            assert not isinstance(col.type, (sa.Float, sa.REAL)), f"{table.name}.{col.name} is float"
    for name in ("subtotal", "tax", "total"):
        t = Base.metadata.tables["extracted_results"].c[name].type
        assert (t.precision, t.scale) == (14, 2)
    for name in ("unit_price", "line_total"):
        t = Base.metadata.tables["line_items"].c[name].type
        assert (t.precision, t.scale) == (14, 2)


@pytest.fixture
def session():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        trans = conn.begin()
        yield Session(conn)
        trans.rollback()  # leave the DB clean


def _doc(h: str) -> Document:
    return Document(original_filename="a.pdf", stored_path="/x/a.pdf", file_type="pdf", file_size=1, file_hash=h)


def test_duplicate_hash_rejected(session):
    session.add(_doc("a" * 64))
    session.flush()
    session.add(_doc("a" * 64))
    with pytest.raises(IntegrityError):
        session.flush()


def test_status_defaults_and_check(session):
    d = _doc("b" * 64)
    session.add(d)
    session.flush()
    session.refresh(d)
    assert d.status == DocumentStatus.queued
    with pytest.raises(IntegrityError):
        session.execute(sa.text("UPDATE documents SET status='bogus'"))


def test_money_roundtrip_exact_and_cascade(session):
    d = _doc("c" * 64)
    r = ExtractedResult(total=Decimal("0.10") + Decimal("0.20"))
    d.pages = []
    session.add(d)
    session.flush()
    r.document_id = d.id
    r.line_items = [LineItem(position=1, unit_price=Decimal("19.99"), line_total=Decimal("19.99"))]
    session.add(r)
    session.flush()
    session.expire_all()
    assert session.get(ExtractedResult, r.id).total == Decimal("0.30")
    session.execute(sa.delete(Document))
    assert session.scalar(sa.select(sa.func.count()).select_from(LineItem)) == 0


def test_vector_search_uses_hnsw_ordering(session):
    d = _doc("d" * 64)
    session.add(d)
    session.flush()
    dim = Embedding.__table__.c.embedding.type.dim
    near = [1.0] + [0.0] * (dim - 1)
    far = [0.0, 1.0] + [0.0] * (dim - 2)
    session.add_all([
        Embedding(document_id=d.id, chunk_index=0, chunk_text="far", embedding=far),
        Embedding(document_id=d.id, chunk_index=1, chunk_text="near", embedding=near),
    ])
    session.flush()
    top = session.scalars(
        sa.select(Embedding).order_by(Embedding.embedding.cosine_distance(near)).limit(1)
    ).one()
    assert top.chunk_text == "near"

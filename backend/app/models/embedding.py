import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.models.base import Base, UUIDPrimaryKey


class Embedding(UUIDPrimaryKey, Base):
    __tablename__ = "embeddings"
    __table_args__ = (
        # (document_id, chunk_index) also serves as the FK index on document_id.
        UniqueConstraint("document_id", "chunk_index", name="uq_embeddings_document_id_chunk_index"),
        Index(
            "ix_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_text: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(Vector(settings.embedding_dimension), nullable=False)

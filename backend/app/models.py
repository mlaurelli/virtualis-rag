"""Modelli ORM SQLAlchemy 2.0 del sistema Virtualis."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base dichiarativa comune a tutti i modelli."""


class StrEnum(str, enum.Enum):  # noqa: UP042 — enum.StrEnum cambierebbe str(member)
    """Enum basato su stringa (serializzabile in JSON/SQLite)."""


class UserRole(StrEnum):
    admin = "admin"
    user = "user"


class SourceType(StrEnum):
    local_fs = "local_fs"
    upload = "upload"
    gdrive = "gdrive"
    m365 = "m365"


class DocumentStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    indexed = "indexed"
    failed = "failed"


class MessageRole(StrEnum):
    user = "user"
    assistant = "assistant"


class AgentPath(StrEnum):
    fast = "fast"
    agentic = "agentic"


class User(Base):
    """Utente del sistema (admin o user)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default=UserRole.user.value)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Source(Base):
    """Sorgente documentale (filesystem locale, upload, Google Drive, M365)."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    type: Mapped[str] = mapped_column(String(16))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    documents: Mapped[list["Document"]] = relationship(back_populates="source")


class Document(Base):
    """Documento indicizzato (o in corso di indicizzazione)."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_document_source_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(1024))  # path locale o id remoto
    title: Mapped[str] = mapped_column(String(512), default="")
    doc_type: Mapped[str] = mapped_column(String(32), default="")
    author: Mapped[str] = mapped_column(String(256), default="")
    language: Mapped[str] = mapped_column(String(16), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    modified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    size_bytes: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(
        String(16), default=DocumentStatus.pending.value, index=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), index=True
    )

    source: Mapped[Source] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    """Frammento di testo di un documento, unità di indicizzazione/retrieval."""

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    ord: Mapped[int] = mapped_column()  # posizione ordinale nel documento
    text: Mapped[str] = mapped_column(Text)
    section_path: Mapped[list[str]] = mapped_column(JSON, default=list)
    page_start: Mapped[int | None] = mapped_column(nullable=True)
    page_end: Mapped[int | None] = mapped_column(nullable=True)
    context_text: Mapped[str] = mapped_column(Text, default="")  # finestra estesa
    token_count: Mapped[int] = mapped_column(default=0)
    # Rettangoli di layout (JSON list[BBox], vuota per formati senza layout)
    bboxes_json: Mapped[str] = mapped_column(Text, default="[]")

    document: Mapped[Document] = relationship(back_populates="chunks")


class ChatSession(Base):
    """Sessione di chat di un utente."""

    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.id"
    )


class ChatMessage(Base):
    """Singolo messaggio (utente o assistente) in una sessione."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    citations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class Feedback(Base):
    """Valutazione di un utente su una risposta dell'assistente.

    Un solo feedback per messaggio (``message_id`` unique): un secondo invio
    sullo stesso messaggio sovrascrive il precedente (upsert lato API).
    ``question`` ed ``answer_excerpt`` sono denormalizzati al momento
    dell'invio, così l'analisi resta leggibile anche se la chat viene
    eliminata o la risposta rigenerata.
    """

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("chat_messages.id"), unique=True, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    rating: Mapped[int] = mapped_column()  # -1 negativo, +1 positivo
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    question: Mapped[str] = mapped_column(Text, default="")  # denormalizzata
    answer_excerpt: Mapped[str] = mapped_column(Text, default="")  # primi 500 char
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    """Traccia di audit di ogni risposta generata (trasperenza e debug)."""

    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("chat_sessions.id"), nullable=True)
    question: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(String(16))  # "fast" | "agentic"
    steps_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    retrieved_json: Mapped[list[int]] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text, default="")
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    latency_ms: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# --- Knowledge graph documentale (GraphRAG) ----------------------------------
# Tabelle create da init_db (SQLite, niente migrazioni): il grafo è un
# sottoprodotto dell'indicizzazione, ricostruibile in qualunque momento con
# scripts/kg_build.py.


class Entity(Base):
    """Entità estratta dai documenti (persona, contratto, progetto, ...).

    ``name`` è la forma normalizzata (lowercase, spazi compattati) usata per
    il match; ``display_name`` conserva la forma originale più leggibile.
    """

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("name", "type", name="uq_entity_name_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(512), index=True)  # normalizzato
    display_name: Mapped[str] = mapped_column(String(512), default="")
    type: Mapped[str] = mapped_column(String(32), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EntityMention(Base):
    """Presenza di un'entità in un documento (ed eventualmente in un chunk)."""

    __tablename__ = "entity_mentions"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    chunk_id: Mapped[int | None] = mapped_column(
        ForeignKey("chunks.id"), nullable=True, index=True
    )


class Relation(Base):
    """Relazione soggetto-predicato-oggetto tra due entità, con provenienza."""

    __tablename__ = "relations"
    __table_args__ = (
        UniqueConstraint(
            "subject_id",
            "predicate",
            "object_id",
            "document_id",
            name="uq_relation_triple_doc",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    predicate: Mapped[str] = mapped_column(String(128))
    object_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    chunk_id: Mapped[int | None] = mapped_column(
        ForeignKey("chunks.id"), nullable=True, index=True
    )

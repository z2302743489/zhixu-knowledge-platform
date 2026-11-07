from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import String, Text, Integer, Boolean, Float, DateTime, JSON, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


def uid(): return str(uuid4())
def now(): return datetime.now(timezone.utc)


class ModelConfig(Base):
    __tablename__ = "model_configs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(40))
    type: Mapped[str] = mapped_column(String(20))
    model_id: Mapped[str] = mapped_column(String(200))
    base: Mapped[str] = mapped_column(String(500))
    dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    encrypted_key: Mapped[str] = mapped_column(Text, default="")
    options: Mapped[dict] = mapped_column(JSON, default=dict)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(80), default="通用资料")
    color: Mapped[str] = mapped_column(String(20), default="violet")
    model_id: Mapped[str] = mapped_column(ForeignKey("model_configs.id", ondelete="RESTRICT"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class DocumentRecord(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("uq_documents_live_kb_sha256", "kb_id", "sha256", unique=True,
              sqlite_where=text("status != 'deleted' AND sha256 != ''"),
              postgresql_where=text("status != 'deleted' AND sha256 != ''")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="processing", index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    path: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    active_version: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChunkRecord(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    version: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    location: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (Index("ix_chunk_document_version", "document_id", "version"),)


class ImportJob(Base):
    __tablename__ = "import_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(50), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Assistant(Base):
    __tablename__ = "assistants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    kb_ids: Mapped[list] = mapped_column(JSON, default=list)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_configs.id", ondelete="RESTRICT"))
    prompt: Mapped[str] = mapped_column(Text, default="请根据资料回答，资料不足时明确说明。")
    greeting: Mapped[str] = mapped_column(Text, default="你好，有什么可以帮你？")
    rerank: Mapped[bool] = mapped_column(Boolean, default=False)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    options: Mapped[dict] = mapped_column(JSON, default=dict)


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner: Mapped[str] = mapped_column(String(36), index=True)
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text, default="")
    refs: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="complete")
    feedback: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LogEntry(Base):
    __tablename__ = "logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="success")
    refs: Mapped[list] = mapped_column(JSON, default=list)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    owner: Mapped[str] = mapped_column(String(36), default="", index=True)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)

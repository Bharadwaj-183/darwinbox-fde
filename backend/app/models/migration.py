from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MigrationRun(Base):
    __tablename__ = "migration_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    status: Mapped[str] = mapped_column(String(40), default="CREATED", index=True)
    target_schema: Mapped[dict] = mapped_column(JSON)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    artifacts: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    files: Mapped[list["MigrationFile"]] = relationship(back_populates="migration", cascade="all, delete-orphan")
    records: Mapped[list["MigrationRecord"]] = relationship(back_populates="migration", cascade="all, delete-orphan")
    escalations: Mapped[list["Escalation"]] = relationship(back_populates="migration", cascade="all, delete-orphan")
    events: Mapped[list["AuditEvent"]] = relationship(back_populates="migration", cascade="all, delete-orphan")


class MigrationFile(Base):
    __tablename__ = "migration_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    migration_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    storage_path: Mapped[str] = mapped_column(Text)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    columns: Mapped[list] = mapped_column(JSON, default=list)

    migration: Mapped[MigrationRun] = relationship(back_populates="files")


class MigrationRecord(Base):
    __tablename__ = "migration_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    migration_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id", ondelete="CASCADE"), index=True)
    entity_name: Mapped[str] = mapped_column(String(120), index=True)
    source_file: Mapped[str] = mapped_column(String(255), index=True)
    source_row_number: Mapped[int] = mapped_column(Integer)
    raw_data: Mapped[dict] = mapped_column(JSON)
    canonical_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="INGESTED", index=True)
    resolution_group: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    target_record_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    target_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    migration: Mapped[MigrationRun] = relationship(back_populates="records")


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    migration_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id", ondelete="CASCADE"), index=True)
    record_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    entity_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(40), default="OPEN", index=True)
    title: Mapped[str] = mapped_column(String(255))
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    suggested_resolution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    migration: Mapped[MigrationRun] = relationship(back_populates="escalations")


class LLMCache(Base):
    __tablename__ = "llm_cache"

    request_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(160))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    migration_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.id", ondelete="CASCADE"), index=True)
    record_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text, default="")
    before_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    migration: Mapped[MigrationRun] = relationship(back_populates="events")

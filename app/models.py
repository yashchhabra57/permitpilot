"""ORM models.

Ordinance -> Chunk -> Requirement is the knowledge side (what the city requires).
Application -> ChecklistItem / ApplicationEvent is the workflow side (one contractor's permit).
Job and Notification back the reminder queue.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.clock import utcnow
from app.db import Base


class Ordinance(Base):
    __tablename__ = "ordinances"

    id: Mapped[int] = mapped_column(primary_key=True)
    jurisdiction: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(200))
    source_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="ordinance", cascade="all, delete-orphan", order_by="Chunk.position"
    )
    requirements: Mapped[list["Requirement"]] = relationship(
        back_populates="ordinance", cascade="all, delete-orphan", order_by="Requirement.id"
    )


class Chunk(Base):
    """One section of an ordinance. The unit of retrieval and of citation."""

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    ordinance_id: Mapped[int] = mapped_column(ForeignKey("ordinances.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(40))
    heading: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)

    ordinance: Mapped[Ordinance] = relationship(back_populates="chunks")

    __table_args__ = (Index("ix_chunks_ordinance_position", "ordinance_id", "position"),)


class Requirement(Base):
    __tablename__ = "requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    ordinance_id: Mapped[int] = mapped_column(ForeignKey("ordinances.id", ondelete="CASCADE"))
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"))
    # "*" means the requirement applies to every permit type in the ordinance.
    permit_type: Mapped[str] = mapped_column(String(40), index=True)
    category: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    quote: Mapped[str] = mapped_column(Text)
    mandatory: Mapped[bool] = mapped_column(Boolean, default=True)
    extractor: Mapped[str] = mapped_column(String(20))

    ordinance: Mapped[Ordinance] = relationship(back_populates="requirements")
    chunk: Mapped[Chunk] = relationship()


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    ordinance_id: Mapped[int] = mapped_column(ForeignKey("ordinances.id"))
    permit_type: Mapped[str] = mapped_column(String(40))
    project_address: Mapped[str] = mapped_column(String(200))
    contractor_name: Mapped[str] = mapped_column(String(120))
    contractor_email: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    # Optimistic concurrency: every transition must name the version it read.
    version: Mapped[int] = mapped_column(Integer, default=1)
    permit_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    inspection_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    ordinance: Mapped[Ordinance] = relationship()
    checklist: Mapped[list["ChecklistItem"]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="ChecklistItem.id"
    )
    events: Mapped[list["ApplicationEvent"]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="ApplicationEvent.id"
    )

    # The ORM adds "WHERE version = <loaded version>" to every UPDATE and bumps it, so two
    # concurrent edits cannot both win (the loser gets StaleDataError).
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class ChecklistItem(Base):
    __tablename__ = "checklist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    requirement_id: Mapped[int | None] = mapped_column(
        ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    citation: Mapped[str] = mapped_column(String(80))
    mandatory: Mapped[bool] = mapped_column(Boolean, default=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    application: Mapped[Application] = relationship(back_populates="checklist")


class ApplicationEvent(Base):
    """Append-only audit trail of every workflow transition."""

    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(40))
    from_state: Mapped[str] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="events")


class Job(Base):
    """A unit of background work. The table is the queue."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    run_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_jobs_dedupe_key"),
        # The worker's hot query: "queued jobs whose run_at has passed, oldest first".
        Index("ix_jobs_status_run_at", "status", "run_at"),
    )


class Notification(Base):
    """Outbox of messages the worker produced. A delivery adapter would read from here."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=True, index=True
    )
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recipient: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

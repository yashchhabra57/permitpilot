"""Use cases that combine the pieces: ingest an ordinance, open an application, tick a box."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chunking import chunk_ordinance
from app.extraction.base import PERMIT_TYPES, ChunkInput, Extractor
from app.extraction.pipeline import ExtractionResult, run_extraction
from app.models import Application, ChecklistItem, Chunk, Ordinance, Requirement
from app.retrieval import BM25Index, best_sentences


class NotFound(Exception):
    pass


class InvalidInput(Exception):
    pass


def ingest_ordinance(
    session: Session, jurisdiction: str, title: str, text: str, llm: Extractor | None
) -> tuple[Ordinance, ExtractionResult]:
    drafts = chunk_ordinance(text)
    if not any(d.section != "preamble" for d in drafts):
        raise InvalidInput("no numbered sections found (expected headings like '§ 14-201 Decks.')")
    ordinance = Ordinance(jurisdiction=jurisdiction.strip(), title=title.strip(), source_text=text)
    ordinance.chunks = [
        Chunk(position=d.position, section=d.section, heading=d.heading, text=d.text)
        for d in drafts
    ]
    session.add(ordinance)
    session.flush()  # assign chunk ids

    inputs = [ChunkInput(c.id, c.section, c.heading, c.text) for c in ordinance.chunks]
    result = run_extraction(inputs, llm)
    ordinance.requirements = [
        Requirement(
            chunk_id=r.chunk_key,
            permit_type=r.permit_type,
            category=r.category,
            description=r.description,
            quote=r.quote,
            mandatory=r.mandatory,
            extractor=result.extractor,
        )
        for r in result.requirements
    ]
    session.flush()
    return ordinance, result


def permit_types_for(ordinance: Ordinance) -> list[str]:
    found = {r.permit_type for r in ordinance.requirements if r.permit_type != "*"}
    return [t for t in PERMIT_TYPES if t in found]


def create_application(
    session: Session,
    ordinance_id: int,
    permit_type: str,
    project_address: str,
    contractor_name: str,
    contractor_email: str,
    now: datetime,
) -> Application:
    ordinance = session.get(Ordinance, ordinance_id)
    if ordinance is None:
        raise NotFound(f"ordinance {ordinance_id} not found")
    if permit_type not in permit_types_for(ordinance):
        raise InvalidInput(
            f"ordinance {ordinance_id} has no requirements for permit type {permit_type!r}"
        )
    app = Application(
        ordinance=ordinance,
        permit_type=permit_type,
        project_address=project_address.strip(),
        contractor_name=contractor_name.strip(),
        contractor_email=contractor_email.strip(),
        state="draft",
        created_at=now,
        updated_at=now,
    )
    app.checklist = [
        ChecklistItem(
            requirement_id=r.id,
            category=r.category,
            description=r.description,
            citation=f"§ {r.chunk.section}",
            mandatory=r.mandatory,
        )
        for r in ordinance.requirements
        if r.permit_type in (permit_type, "*")
    ]
    session.add(app)
    session.flush()
    return app


def set_item_done(
    session: Session, application_id: int, item_id: int, done: bool, now: datetime
) -> ChecklistItem:
    item = session.scalar(
        select(ChecklistItem).where(
            ChecklistItem.id == item_id, ChecklistItem.application_id == application_id
        )
    )
    if item is None:
        raise NotFound(f"checklist item {item_id} not found on application {application_id}")
    if item.application.state in ("closed", "rejected", "withdrawn"):
        raise InvalidInput(f"application is {item.application.state}; checklist is locked")
    item.done = done
    item.done_at = now if done else None
    session.flush()
    return item


@dataclass
class SearchHit:
    ordinance_id: int
    jurisdiction: str
    section: str
    heading: str
    score: float
    excerpt: list[str]
    text: str


def search_chunks(
    session: Session, query: str, ordinance_id: int | None = None, k: int = 5
) -> list[SearchHit]:
    stmt = select(Chunk).join(Ordinance)
    if ordinance_id is not None:
        stmt = stmt.where(Chunk.ordinance_id == ordinance_id)
    chunks = {c.id: c for c in session.scalars(stmt)}
    # Headings carry strong signal ("Fences"), so they are indexed with the body.
    index = BM25Index([(cid, f"{c.heading} {c.text}") for cid, c in chunks.items()])
    hits = []
    for hit in index.search(query, k):
        c = chunks[hit.key]
        hits.append(
            SearchHit(
                ordinance_id=c.ordinance_id,
                jurisdiction=c.ordinance.jurisdiction,
                section=c.section,
                heading=c.heading,
                score=round(hit.score, 4),
                excerpt=best_sentences(query, c.text),
                text=c.text,
            )
        )
    return hits

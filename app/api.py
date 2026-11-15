"""HTTP routes. Each handler validates input, calls a service, and commits once."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import schemas
from app.clock import Clock
from app.config import Settings
from app.extraction.llm import build_llm_extractor
from app.jobs import run_due
from app.models import Application, Job, Notification, Ordinance, Requirement
from app.reminders import HANDLERS
from app.services import (
    create_application,
    ingest_ordinance,
    permit_types_for,
    search_chunks,
    set_item_done,
)
from app.workflow import TransitionInput, apply_transition, available_actions

router = APIRouter(prefix="/api")


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SessionDep = Annotated[Session, Depends(get_session)]
ClockDep = Annotated[Clock, Depends(get_clock)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def _ordinance_out(o: Ordinance) -> schemas.OrdinanceOut:
    return schemas.OrdinanceOut(
        id=o.id,
        jurisdiction=o.jurisdiction,
        title=o.title,
        permit_types=permit_types_for(o),
        sections=len(o.chunks),
        requirements=len(o.requirements),
    )


def _load_application(session: Session, application_id: int) -> Application:
    app = session.scalar(
        select(Application)
        .where(Application.id == application_id)
        .options(selectinload(Application.checklist), selectinload(Application.events))
    )
    if app is None:
        raise HTTPException(404, f"application {application_id} not found")
    return app


def _application_out(session: Session, app: Application, now: datetime) -> schemas.ApplicationOut:
    jobs = session.scalars(select(Job).where(Job.application_id == app.id).order_by(Job.run_at))
    mandatory = [i for i in app.checklist if i.mandatory]
    return schemas.ApplicationOut(
        **schemas.ApplicationSummary.model_validate(app).model_dump(),
        contractor_email=app.contractor_email,
        issued_at=app.issued_at,
        expires_at=app.expires_at,
        inspection_at=app.inspection_at,
        created_at=app.created_at,
        progress={"done": sum(i.done for i in mandatory), "total": len(mandatory)},
        checklist=[schemas.ChecklistItemOut.model_validate(i) for i in app.checklist],
        events=[schemas.EventOut.model_validate(e) for e in app.events],
        actions=[
            schemas.ActionOut(action=a.action, target=a.target, blocked_by=a.blocked_by)
            for a in available_actions(app, now)
        ],
        jobs=[schemas.JobOut.model_validate(j) for j in jobs],
    )


# ---- ordinances -------------------------------------------------------------------------------


@router.get("/ordinances", response_model=list[schemas.OrdinanceOut])
def list_ordinances(session: SessionDep) -> list[schemas.OrdinanceOut]:
    rows = session.scalars(select(Ordinance).order_by(Ordinance.id))
    return [_ordinance_out(o) for o in rows]


@router.post("/ordinances", response_model=schemas.IngestOut, status_code=201)
def post_ordinance(
    body: schemas.OrdinanceIn, session: SessionDep, settings: SettingsDep, request: Request
) -> schemas.IngestOut:
    llm = getattr(request.app.state, "llm_extractor", None) or build_llm_extractor(
        settings.anthropic_api_key, settings.llm_model
    )
    ordinance, result = ingest_ordinance(session, body.jurisdiction, body.title, body.text, llm)
    session.commit()
    return schemas.IngestOut(
        **_ordinance_out(ordinance).model_dump(),
        extractor=result.extractor,
        fallback_reason=result.fallback_reason,
    )


@router.get("/ordinances/{ordinance_id}/requirements", response_model=list[schemas.RequirementOut])
def list_requirements(
    ordinance_id: int, session: SessionDep, permit_type: str | None = None
) -> list[schemas.RequirementOut]:
    if session.get(Ordinance, ordinance_id) is None:
        raise HTTPException(404, f"ordinance {ordinance_id} not found")
    stmt = select(Requirement).where(Requirement.ordinance_id == ordinance_id)
    if permit_type:
        stmt = stmt.where(Requirement.permit_type.in_([permit_type, "*"]))
    return [
        schemas.RequirementOut(
            id=r.id,
            permit_type=r.permit_type,
            category=r.category,
            description=r.description,
            quote=r.quote,
            section=r.chunk.section,
            extractor=r.extractor,
        )
        for r in session.scalars(stmt.order_by(Requirement.id))
    ]


@router.get("/search", response_model=list[schemas.SearchHitOut])
def search(
    session: SessionDep,
    q: Annotated[str, Query(min_length=2, max_length=300)],
    ordinance_id: int | None = None,
    k: Annotated[int, Query(ge=1, le=20)] = 5,
) -> list[schemas.SearchHitOut]:
    return [
        schemas.SearchHitOut(
            ordinance_id=h.ordinance_id,
            jurisdiction=h.jurisdiction,
            section=h.section,
            heading=h.heading,
            score=h.score,
            excerpt=h.excerpt,
        )
        for h in search_chunks(session, q, ordinance_id, k)
    ]


# ---- applications -----------------------------------------------------------------------------


@router.post("/applications", response_model=schemas.ApplicationOut, status_code=201)
def post_application(
    body: schemas.ApplicationIn, session: SessionDep, clock: ClockDep
) -> schemas.ApplicationOut:
    now = clock()
    app = create_application(
        session,
        body.ordinance_id,
        body.permit_type,
        body.project_address,
        body.contractor_name,
        body.contractor_email,
        now,
    )
    session.commit()
    return _application_out(session, app, now)


@router.get("/applications", response_model=list[schemas.ApplicationSummary])
def list_applications(
    session: SessionDep, state: str | None = None
) -> list[schemas.ApplicationSummary]:
    stmt = select(Application).order_by(Application.updated_at.desc(), Application.id.desc())
    if state:
        stmt = stmt.where(Application.state == state)
    return [schemas.ApplicationSummary.model_validate(a) for a in session.scalars(stmt)]


@router.get("/applications/{application_id}", response_model=schemas.ApplicationOut)
def get_application(
    application_id: int, session: SessionDep, clock: ClockDep
) -> schemas.ApplicationOut:
    return _application_out(session, _load_application(session, application_id), clock())


@router.patch(
    "/applications/{application_id}/checklist/{item_id}", response_model=schemas.ApplicationOut
)
def patch_checklist_item(
    application_id: int,
    item_id: int,
    body: schemas.ChecklistPatch,
    session: SessionDep,
    clock: ClockDep,
) -> schemas.ApplicationOut:
    now = clock()
    set_item_done(session, application_id, item_id, body.done, now)
    session.commit()
    return _application_out(session, _load_application(session, application_id), now)


@router.post("/applications/{application_id}/transitions", response_model=schemas.ApplicationOut)
def post_transition(
    application_id: int, body: schemas.TransitionIn, session: SessionDep, clock: ClockDep
) -> schemas.ApplicationOut:
    now = clock()
    app = _load_application(session, application_id)
    apply_transition(
        session,
        app,
        body.action,
        body.expected_version,
        now,
        TransitionInput(note=body.note, inspection_at=body.inspection_at),
    )
    session.commit()
    return _application_out(session, _load_application(session, application_id), now)


# ---- jobs and notifications -------------------------------------------------------------------


@router.get("/jobs", response_model=list[schemas.JobOut])
def list_jobs(session: SessionDep, status: str | None = None) -> list[schemas.JobOut]:
    stmt = select(Job).order_by(Job.run_at, Job.id)
    if status:
        stmt = stmt.where(Job.status == status)
    return [schemas.JobOut.model_validate(j) for j in session.scalars(stmt.limit(500))]


@router.get("/jobs/stats")
def job_stats(session: SessionDep) -> dict[str, int]:
    rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    return {status: count for status, count in rows}


@router.post("/jobs/run-due", response_model=schemas.RunDueOut)
def post_run_due(
    request: Request,
    settings: SettingsDep,
    clock: ClockDep,
    as_of: datetime | None = None,
) -> schemas.RunDueOut:
    """Run the worker once, in-process. `as_of` lets a demo fast-forward time."""
    if as_of is not None and not settings.demo_mode:
        raise HTTPException(403, "as_of is only allowed in demo mode")
    if as_of is not None and as_of.tzinfo is not None:
        as_of = as_of.astimezone(UTC).replace(tzinfo=None)  # store everything as naive UTC
    when = as_of or clock()
    stats = run_due(
        request.app.state.session_factory,
        HANDLERS,
        "api",
        when,
        lease_seconds=settings.job_lease_seconds,
        backoff_base=settings.job_backoff_base_seconds,
        backoff_cap=settings.job_backoff_cap_seconds,
    )
    return schemas.RunDueOut(
        as_of=when,
        claimed=stats.claimed,
        done=stats.done,
        retried=stats.retried,
        dead=stats.dead,
        errors=stats.errors,
    )


@router.get("/notifications", response_model=list[schemas.NotificationOut])
def list_notifications(
    session: SessionDep, application_id: int | None = None
) -> list[schemas.NotificationOut]:
    stmt = select(Notification).order_by(Notification.id.desc())
    if application_id is not None:
        stmt = stmt.where(Notification.application_id == application_id)
    return [schemas.NotificationOut.model_validate(n) for n in session.scalars(stmt.limit(200))]

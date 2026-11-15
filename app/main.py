"""FastAPI application factory."""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

from app.api import router
from app.clock import Clock, utcnow
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_session_factory
from app.extraction.base import Extractor
from app.models import Ordinance
from app.services import InvalidInput, NotFound, ingest_ordinance
from app.workflow import WorkflowError

log = logging.getLogger(__name__)

SEED_FILE = Path(__file__).parent / "seed" / "lakeport_residential.txt"

# Workflow error code -> HTTP status.
WORKFLOW_STATUS = {
    "unknown_action": 400,
    "invalid_transition": 409,
    "stale_version": 409,
    "note_required": 422,
    "guard_failed": 422,
}


def _error(status: int, code: str, message: str, details: list[str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"code": code, "message": message, "details": details or []}
    )


def seed(app: FastAPI) -> None:
    with app.state.session_factory() as session:
        if session.scalar(select(Ordinance.id).limit(1)) is not None:
            return
        ingest_ordinance(
            session,
            "Lakeport (sample)",
            "Chapter 14: Residential Building Permits",
            SEED_FILE.read_text(encoding="utf-8"),
            None,  # seed with the deterministic extractor so startup never calls an API
        )
        session.commit()
        log.info("seeded sample ordinance")


def create_app(
    settings: Settings | None = None,
    clock: Clock = utcnow,
    llm_extractor: Extractor | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="PermitPilot",
        version="0.1.0",
        description="Permit checklists from city ordinances, application workflow tracking, "
        "and reminder jobs for small contractors.",
    )
    engine = make_engine(settings.database_url)
    init_db(engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.clock = clock
    app.state.llm_extractor = llm_extractor
    if settings.seed_on_startup:
        seed(app)

    @app.exception_handler(WorkflowError)
    def workflow_error(_: Request, exc: WorkflowError) -> JSONResponse:
        return _error(WORKFLOW_STATUS.get(exc.code, 400), exc.code, exc.message, exc.details)

    @app.exception_handler(NotFound)
    def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(InvalidInput)
    def invalid(_: Request, exc: InvalidInput) -> JSONResponse:
        return _error(422, "invalid_input", str(exc))

    @app.exception_handler(StaleDataError)
    def stale(_: Request, exc: StaleDataError) -> JSONResponse:
        return _error(409, "stale_version", "application was changed by another request")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app

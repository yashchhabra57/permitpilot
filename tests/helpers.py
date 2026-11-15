from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Application, Ordinance
from app.services import create_application, ingest_ordinance

SAMPLE = Path(__file__).parents[1] / "app" / "seed" / "lakeport_residential.txt"
T0 = datetime(2026, 3, 2, 9, 0, 0)


def seed_ordinance(session: Session) -> Ordinance:
    ordinance, _ = ingest_ordinance(
        session, "Lakeport (sample)", "Chapter 14", SAMPLE.read_text(encoding="utf-8"), None
    )
    session.commit()
    return ordinance


def new_application(session: Session, permit_type: str = "deck") -> Application:
    ordinance = seed_ordinance(session)
    app = create_application(
        session, ordinance.id, permit_type, "12 Pine St", "Ana Builder", "ana@example.com", T0
    )
    session.commit()
    return app


def tick(app: Application, *categories: str) -> None:
    for item in app.checklist:
        if item.category in categories:
            item.done = True

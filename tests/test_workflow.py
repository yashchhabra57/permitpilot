from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.models import Application, Job
from app.workflow import (
    TRANSITIONS,
    TransitionInput,
    WorkflowError,
    apply_transition,
    available_actions,
)
from tests.helpers import T0, new_application, tick


def step(
    session: Session, app: Application, action: str, days: int = 0, **data: object
) -> Application:
    apply_transition(
        session, app, action, app.version, T0 + timedelta(days=days), TransitionInput(**data)  # type: ignore[arg-type]
    )
    session.commit()
    return app


def jobs(session: Session, app: Application) -> dict[str, str]:
    rows = session.scalars(
        select(Job)
        .where(Job.application_id == app.id)
        .order_by(Job.id)
        .execution_options(populate_existing=True)
    )
    return {r.dedupe_key.split(":")[2]: r.status for r in rows if r.dedupe_key}


def test_checklist_is_built_from_matching_requirements(session: Session) -> None:
    app = new_application(session, "deck")
    citations = {i.citation for i in app.checklist}
    assert "§ 14-201" in citations and "§ 14-103" in citations
    assert "§ 14-301" not in citations  # fence rules are not on a deck checklist


def test_submit_is_blocked_until_documents_are_done(session: Session) -> None:
    app = new_application(session)
    with pytest.raises(WorkflowError) as err:
        step(session, app, "submit")
    assert err.value.code == "guard_failed"
    assert any("framing plans" in d for d in err.value.details)
    assert app.state == "draft"

    tick(app, "document")
    step(session, app, "submit")
    assert app.state == "submitted"


def test_full_happy_path_creates_events_permit_and_reminders(session: Session) -> None:
    app = new_application(session)
    tick(app, "document")
    step(session, app, "submit")
    step(session, app, "start_review", days=1)
    step(session, app, "approve", days=3)
    with pytest.raises(WorkflowError):  # fee not paid yet
        step(session, app, "issue", days=3)
    tick(app, "fee")
    step(session, app, "issue", days=4)

    assert app.permit_number == f"LP-2026-{app.id:05d}"
    assert app.expires_at == app.issued_at + timedelta(days=180)  # type: ignore[operator]
    assert jobs(session, app) == {
        "start_work_deadline": "queued",
        "final_inspection_deadline": "queued",
    }

    step(session, app, "schedule_inspection", days=10, inspection_at=T0 + timedelta(days=12))
    assert jobs(session, app)["start_work_deadline"] == "cancelled"
    assert jobs(session, app)["inspection_tomorrow"] == "queued"

    with pytest.raises(WorkflowError) as err:
        step(session, app, "pass_inspection", days=12)
    assert err.value.code == "guard_failed"
    tick(app, "inspection")
    step(session, app, "pass_inspection", days=12)

    assert app.state == "closed"
    assert set(jobs(session, app).values()) == {"cancelled"}
    assert [e.action for e in app.events] == [
        "submit",
        "start_review",
        "approve",
        "issue",
        "schedule_inspection",
        "pass_inspection",
    ]


def test_corrections_round_trip_schedules_and_cancels_reminder(session: Session) -> None:
    app = new_application(session)
    tick(app, "document")
    step(session, app, "submit")
    step(session, app, "start_review")
    with pytest.raises(WorkflowError) as err:
        step(session, app, "request_corrections")
    assert err.value.code == "note_required"

    step(session, app, "request_corrections", days=2, note="Footing detail missing")
    reminder = session.scalar(select(Job).where(Job.application_id == app.id))
    assert reminder is not None
    assert reminder.run_at == T0 + timedelta(days=2 + 23)

    step(session, app, "submit", days=5)
    assert jobs(session, app) == {"corrections_due": "cancelled"}
    assert app.events[2].note == "Footing detail missing"


def test_second_corrections_round_gets_its_own_reminder(session: Session) -> None:
    app = new_application(session)
    tick(app, "document")
    step(session, app, "submit")
    for _ in range(2):
        step(session, app, "start_review")
        step(session, app, "request_corrections", note="more")
        step(session, app, "submit")
    keys = session.scalars(select(Job.dedupe_key).where(Job.application_id == app.id)).all()
    assert len(set(keys)) == 2


def test_invalid_transition_and_unknown_action(session: Session) -> None:
    app = new_application(session)
    with pytest.raises(WorkflowError) as err:
        step(session, app, "approve")
    assert err.value.code == "invalid_transition"
    with pytest.raises(WorkflowError) as err:
        step(session, app, "teleport")
    assert err.value.code == "unknown_action"


def test_stale_version_is_rejected(session: Session) -> None:
    app = new_application(session)
    tick(app, "document")
    old_version = app.version
    step(session, app, "submit")
    with pytest.raises(WorkflowError) as err:
        apply_transition(session, app, "withdraw", old_version, T0)
    assert err.value.code == "stale_version"


def test_concurrent_writers_cannot_both_commit(session: Session) -> None:
    """The ORM version column catches a race the explicit check cannot see."""
    app = new_application(session)
    # Simulate another process bumping the row after we loaded it.
    session.execute(
        Application.__table__.update()
        .where(Application.id == app.id)
        .values(version=app.version + 1)
    )
    with pytest.raises(StaleDataError):
        # Raised at the first flush, which may happen inside the transition or at commit.
        apply_transition(session, app, "withdraw", app.version, T0)
        session.commit()


def test_schedule_inspection_validates_date(session: Session) -> None:
    app = new_application(session)
    app.state = "issued"
    app.expires_at = T0 + timedelta(days=180)
    session.commit()
    with pytest.raises(WorkflowError) as err:
        step(session, app, "schedule_inspection", inspection_at=T0 - timedelta(days=1))
    assert err.value.details == ["inspection_at must be in the future"]
    with pytest.raises(WorkflowError) as err:
        step(session, app, "schedule_inspection", inspection_at=T0 + timedelta(days=400))
    assert err.value.details == ["inspection_at is after the permit expires"]


def test_failed_inspection_returns_to_issued(session: Session) -> None:
    app = new_application(session)
    app.state = "issued"
    session.commit()
    step(session, app, "schedule_inspection", inspection_at=T0 + timedelta(days=2))
    step(session, app, "fail_inspection", days=2, note="Guard too low")
    assert app.state == "issued"
    assert app.inspection_at is None
    assert jobs(session, app)["inspection_tomorrow"] == "cancelled"


def test_withdraw_from_any_open_state_but_not_terminal(session: Session) -> None:
    app = new_application(session)
    step(session, app, "withdraw")
    assert app.state == "withdrawn"
    with pytest.raises(WorkflowError):
        step(session, app, "withdraw")


def test_available_actions_report_blockers(session: Session) -> None:
    app = new_application(session)
    actions = {a.action: a for a in available_actions(app, T0)}
    assert set(actions) == {"submit", "withdraw"}
    assert actions["submit"].blocked_by
    assert actions["withdraw"].blocked_by == []


def test_every_transition_targets_a_known_state() -> None:
    from app.workflow import STATES

    for t in TRANSITIONS.values():
        assert t.target in STATES
        assert t.sources <= set(STATES)

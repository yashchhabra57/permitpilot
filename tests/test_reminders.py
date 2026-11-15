from datetime import timedelta

import pytest
from sqlalchemy import Engine, select

from app.db import make_session_factory
from app.jobs import PermanentJobError, claim, enqueue, run_due
from app.models import Application, Job, Notification
from app.reminders import HANDLERS, handle_reminder
from app.workflow import TransitionInput, apply_transition
from tests.helpers import T0, new_application, tick


def run(engine: Engine, days: int) -> None:
    run_due(
        make_session_factory(engine),
        HANDLERS,
        "test-worker",
        T0 + timedelta(days=days),
        lease_seconds=60,
        backoff_base=30,
        backoff_cap=3600,
    )


def issued_application(engine: Engine) -> int:
    factory = make_session_factory(engine)
    with factory() as s:
        app = new_application(s)
        tick(app, "document", "fee")
        for action in ("submit", "start_review", "approve", "issue"):
            apply_transition(s, app, action, app.version, T0)
            s.commit()
        return app.id


def notifications(engine: Engine) -> list[Notification]:
    with make_session_factory(engine)() as s:
        return list(s.scalars(select(Notification).order_by(Notification.id)))


def test_reminders_fire_only_when_due(engine: Engine) -> None:
    issued_application(engine)
    run(engine, days=149)
    assert notifications(engine) == []
    run(engine, days=150)
    [n] = notifications(engine)
    assert n.subject.startswith("Permit LP-2026-")
    assert "expires in 30 days" in n.subject
    assert n.recipient == "ana@example.com"


def test_reminder_is_skipped_when_state_moved_on(engine: Engine) -> None:
    app_id = issued_application(engine)
    with make_session_factory(engine)() as s:
        app = s.get(Application, app_id)
        assert app is not None
        # Withdraw directly without the workflow so the jobs stay queued.
        app.state = "withdrawn"
        s.commit()
    run(engine, days=400)
    assert notifications(engine) == []
    with make_session_factory(engine)() as s:
        assert {j.status for j in s.scalars(select(Job))} == {"done"}


def test_handler_is_idempotent_on_redelivery(engine: Engine) -> None:
    issued_application(engine)
    factory = make_session_factory(engine)
    when = T0 + timedelta(days=150)
    with factory() as s:
        [job] = claim(s, "w1", when, 60, limit=1)
        handle_reminder(s, job, when)
        s.commit()  # notification written, but the worker "crashes" before complete()
    with factory() as s:
        [again] = claim(s, "w2", when + timedelta(seconds=61), 60, limit=1)
        assert again.id == job.id
        handle_reminder(s, again, when)
        s.commit()
    assert len(notifications(engine)) == 1


def test_bad_payload_is_permanent(engine: Engine) -> None:
    with make_session_factory(engine)() as s:
        job = enqueue(s, "reminder", {"template": "nope"}, T0)
        s.commit()
        with pytest.raises(PermanentJobError):
            handle_reminder(s, job, T0)


def test_inspection_reminder_text(engine: Engine) -> None:
    app_id = issued_application(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        app = s.get(Application, app_id)
        assert app is not None
        apply_transition(
            s,
            app,
            "schedule_inspection",
            app.version,
            T0 + timedelta(days=5),
            TransitionInput(inspection_at=T0 + timedelta(days=10, hours=5)),
        )
        s.commit()
    run(engine, days=10)  # reminder is due 24h before 14:00 on day 10
    [n] = notifications(engine)
    assert n.subject == "Inspection tomorrow at 12 Pine St"
    assert "2026-03-12 14:00" in n.body

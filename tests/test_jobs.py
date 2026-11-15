from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db import make_session_factory
from app.jobs import (
    PermanentJobError,
    backoff_seconds,
    claim,
    complete,
    enqueue,
    fail,
    run_due,
)
from app.models import Job, Notification

T0 = datetime(2026, 1, 1, 9, 0, 0)


def test_backoff_doubles_and_caps() -> None:
    assert [backoff_seconds(a, 30, 200) for a in (1, 2, 3, 4, 5)] == [30, 60, 120, 200, 200]


def test_enqueue_dedupes_by_key(session: Session) -> None:
    a = enqueue(session, "reminder", {"x": 1}, T0, dedupe_key="app:1:expiry")
    session.commit()
    b = enqueue(session, "reminder", {"x": 2}, T0, dedupe_key="app:1:expiry")
    session.commit()
    assert a.id == b.id
    assert session.query(Job).count() == 1


def test_claim_only_due_jobs_in_run_at_order(session: Session) -> None:
    late = enqueue(session, "k", {}, T0 + timedelta(minutes=5))
    first = enqueue(session, "k", {}, T0 - timedelta(minutes=2))
    second = enqueue(session, "k", {}, T0 - timedelta(minutes=1))
    session.commit()

    jobs = claim(session, "w1", T0, lease_seconds=60)
    assert [j.id for j in jobs] == [first.id, second.id]
    assert all(j.status == "running" and j.attempts == 1 for j in jobs)
    assert late.id not in [j.id for j in jobs]


def test_a_claimed_job_cannot_be_claimed_twice(session: Session) -> None:
    enqueue(session, "k", {}, T0)
    session.commit()
    assert len(claim(session, "w1", T0, 60)) == 1
    assert claim(session, "w2", T0 + timedelta(seconds=30), 60) == []


def test_expired_lease_is_reclaimed_and_old_worker_loses_it(session: Session) -> None:
    enqueue(session, "k", {}, T0)
    session.commit()
    [job] = claim(session, "w1", T0, 60)

    # w1 stalls past its lease; w2 takes over.
    [again] = claim(session, "w2", T0 + timedelta(seconds=61), 60)
    assert again.id == job.id
    assert again.attempts == 2

    assert complete(session, job, "w1", T0 + timedelta(seconds=62)) is False
    assert complete(session, again, "w2", T0 + timedelta(seconds=62)) is True
    session.expire_all()
    assert session.get(Job, job.id).status == "done"  # type: ignore[union-attr]


def test_fail_retries_with_backoff_then_dead_letters(session: Session) -> None:
    job = enqueue(session, "k", {}, T0, max_attempts=3)
    session.commit()
    now = T0
    statuses = []
    for _ in range(3):
        [claimed] = claim(session, "w", now, 60)
        statuses.append(fail(session, claimed, "w", "boom", now, base=10, cap=1000))
        session.expire_all()
        refreshed = session.get(Job, job.id)
        assert refreshed is not None
        now = refreshed.run_at
    assert statuses == ["queued", "queued", "dead"]
    final = session.get(Job, job.id)
    assert final is not None and final.attempts == 3 and final.last_error == "boom"


def test_retry_delay_follows_backoff(session: Session) -> None:
    job = enqueue(session, "k", {}, T0)
    session.commit()
    [claimed] = claim(session, "w", T0, 60)
    fail(session, claimed, "w", "boom", T0, base=30, cap=3600)
    session.expire_all()
    assert session.get(Job, job.id).run_at == T0 + timedelta(seconds=30)  # type: ignore[union-attr]


def test_run_due_commits_handler_writes_with_completion(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with factory() as s:
        enqueue(s, "note", {"to": "a@example.com"}, T0)
        enqueue(s, "explode", {}, T0)
        enqueue(s, "permanent", {}, T0)
        enqueue(s, "unknown-kind", {}, T0)
        s.commit()

    def note(s: Session, job: Job, now: datetime) -> None:
        s.add(Notification(recipient="a@example.com", subject="hi", body="b", job_id=job.id))

    def explode(s: Session, job: Job, now: datetime) -> None:
        s.add(Notification(recipient="x", subject="never", body="b", job_id=job.id))
        raise RuntimeError("transient")

    def permanent(s: Session, job: Job, now: datetime) -> None:
        raise PermanentJobError("bad payload")

    stats = run_due(
        factory,
        {"note": note, "explode": explode, "permanent": permanent},
        "w",
        T0,
        lease_seconds=60,
        backoff_base=30,
        backoff_cap=3600,
    )
    assert (stats.claimed, stats.done, stats.retried, stats.dead) == (4, 1, 1, 2)

    with factory() as s:
        subjects = [n.subject for n in s.scalars(select(Notification))]
        assert subjects == ["hi"]  # the failed handler's write was rolled back
        by_kind = {j.kind: j.status for j in s.scalars(select(Job))}
        assert by_kind == {
            "note": "done",
            "explode": "queued",
            "permanent": "dead",
            "unknown-kind": "dead",
        }


@pytest.mark.parametrize("limit", [1, 2])
def test_claim_respects_limit(session: Session, limit: int) -> None:
    for _ in range(3):
        enqueue(session, "k", {}, T0)
    session.commit()
    assert len(claim(session, "w", T0, 60, limit=limit)) == limit

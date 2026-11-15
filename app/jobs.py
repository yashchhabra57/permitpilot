"""A durable job queue stored in a database table.

Why not Redis? Reminders are low-volume and must survive restarts, and the application already
has a database. Keeping jobs in the same database means a job can be created in the same
transaction as the state change that caused it: a permit is never marked "issued" without its
expiry reminders, and vice versa.

How a job moves:

    queued --claim--> running --complete--> done
       ^                 |
       +----fail(retry)--+--fail(no attempts left)--> dead
    queued --cancel--> cancelled

Claiming is a compare-and-set UPDATE: "set status=running WHERE id=? AND it is still claimable".
The database runs that atomically, so if two workers race for the same job exactly one UPDATE
changes a row. This works the same on SQLite and PostgreSQL, with no SELECT ... FOR UPDATE.

A claim is a lease, not ownership. If a worker crashes, its lease expires and another worker may
claim the job again. Handlers therefore have to be idempotent (see reminders.py).
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models import Job

log = logging.getLogger(__name__)

Handler = Callable[[Session, Job, datetime], None]


class PermanentJobError(Exception):
    """Raise from a handler when retrying cannot help (bad payload, unknown kind)."""


def backoff_seconds(attempt: int, base: int, cap: int) -> int:
    """Exponential backoff: base, 2*base, 4*base, ... capped. attempt is 1-based."""
    return int(min(cap, base * 2 ** max(0, attempt - 1)))


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    run_at: datetime,
    *,
    dedupe_key: str | None = None,
    application_id: int | None = None,
    max_attempts: int = 5,
) -> Job:
    """Add a job. With a dedupe_key, enqueueing the same logical job twice returns the first one.

    Does not commit, so the caller can enqueue in the same transaction as its own changes.
    """
    if dedupe_key is not None:
        existing = session.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if existing is not None:
            return existing
    job = Job(
        kind=kind,
        payload=json.dumps(payload, sort_keys=True),
        status="queued",
        run_at=run_at,
        dedupe_key=dedupe_key,
        application_id=application_id,
        max_attempts=max_attempts,
        attempts=0,
    )
    # If two transactions race past the check above, the unique constraint on dedupe_key rejects
    # the second insert at commit time, so a duplicate can never be stored.
    session.add(job)
    return job


def _claimable(now: datetime) -> Any:
    return or_(
        and_(Job.status == "queued", Job.run_at <= now),
        # A running job whose lease ran out belongs to a worker that died.
        and_(Job.status == "running", Job.locked_until < now),
    )


def claim(
    session: Session, worker_id: str, now: datetime, lease_seconds: int, limit: int = 10
) -> list[Job]:
    candidates = session.scalars(
        select(Job.id).where(_claimable(now)).order_by(Job.run_at, Job.id).limit(limit)
    ).all()
    claimed_ids: list[int] = []
    for job_id in candidates:
        result = session.execute(
            update(Job)
            .where(Job.id == job_id, _claimable(now))
            .values(
                status="running",
                locked_by=worker_id,
                locked_until=now + timedelta(seconds=lease_seconds),
                attempts=Job.attempts + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:  # type: ignore[attr-defined]
            claimed_ids.append(job_id)
    session.commit()
    if not claimed_ids:
        return []
    # populate_existing: the UPDATEs bypassed the ORM, so refresh any Job objects already loaded.
    jobs = session.scalars(
        select(Job)
        .where(Job.id.in_(claimed_ids))
        .order_by(Job.run_at, Job.id)
        .execution_options(populate_existing=True)
    )
    return list(jobs.all())


def complete(session: Session, job: Job, worker_id: str, now: datetime) -> bool:
    """Mark done. Returns False if this worker no longer holds the lease."""
    result = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == "running", Job.locked_by == worker_id)
        .values(status="done", finished_at=now, locked_by=None, locked_until=None)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        # Someone else owns the job now: throw away this worker's handler writes too.
        session.rollback()
        return False
    session.commit()
    return True


def fail(
    session: Session,
    job: Job,
    worker_id: str,
    error: str,
    now: datetime,
    *,
    base: int,
    cap: int,
    permanent: bool = False,
) -> str:
    """Record a failure and either reschedule with backoff or dead-letter. Returns the new status."""
    attempts = session.scalar(select(Job.attempts).where(Job.id == job.id)) or job.attempts
    if permanent or attempts >= job.max_attempts:
        values: dict[str, Any] = {"status": "dead", "finished_at": now}
    else:
        delay = backoff_seconds(attempts, base, cap)
        values = {"status": "queued", "run_at": now + timedelta(seconds=delay)}
    values.update(locked_by=None, locked_until=None, last_error=error[:2000])
    result = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == "running", Job.locked_by == worker_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        return "lease_lost"
    return str(values["status"])


def cancel_pending(session: Session, application_id: int, kinds: set[str] | None = None) -> int:
    """Cancel queued jobs for an application (e.g. reminders after it is withdrawn)."""
    stmt = update(Job).where(Job.application_id == application_id, Job.status == "queued")
    if kinds:
        stmt = stmt.where(Job.kind.in_(kinds))
    result = session.execute(
        stmt.values(status="cancelled").execution_options(synchronize_session=False)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


def cancel_by_dedupe_prefix(session: Session, prefix: str) -> int:
    """Cancel queued jobs whose dedupe_key starts with prefix, e.g. one reminder template."""
    result = session.execute(
        update(Job)
        .where(Job.status == "queued", Job.dedupe_key.startswith(prefix, autoescape=True))
        .values(status="cancelled")
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


@dataclass
class RunStats:
    claimed: int = 0
    done: int = 0
    retried: int = 0
    dead: int = 0
    lease_lost: int = 0
    errors: list[str] = field(default_factory=list)


def run_due(
    factory: sessionmaker[Session],
    handlers: dict[str, Handler],
    worker_id: str,
    now: datetime,
    *,
    lease_seconds: int,
    backoff_base: int,
    backoff_cap: int,
    limit: int = 50,
) -> RunStats:
    """Claim every due job (up to limit) and run it. One session per job keeps failures isolated."""
    stats = RunStats()
    with factory() as s:
        jobs = claim(s, worker_id, now, lease_seconds, limit)
    stats.claimed = len(jobs)
    for job in jobs:
        with factory() as s:
            handler = handlers.get(job.kind)
            try:
                if handler is None:
                    raise PermanentJobError(f"no handler for job kind {job.kind!r}")
                handler(s, job, now)
                s.flush()
            except Exception as exc:
                s.rollback()
                permanent = isinstance(exc, PermanentJobError)
                status = fail(
                    s,
                    job,
                    worker_id,
                    f"{type(exc).__name__}: {exc}",
                    now,
                    base=backoff_base,
                    cap=backoff_cap,
                    permanent=permanent,
                )
                stats.errors.append(f"job {job.id}: {exc}")
                if status == "dead":
                    stats.dead += 1
                elif status == "queued":
                    stats.retried += 1
                else:
                    stats.lease_lost += 1
                continue
            # The handler's writes and the "done" mark commit together.
            if complete(s, job, worker_id, now):
                stats.done += 1
            else:
                stats.lease_lost += 1
                log.warning("job %s finished after its lease expired", job.id)
    return stats

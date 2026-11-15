from pathlib import Path

import pytest

from app import worker
from app.config import get_settings
from app.db import init_db, make_engine, make_session_factory
from app.jobs import enqueue
from app.models import Job
from tests.helpers import T0


def test_run_once_processes_due_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{(tmp_path / 'worker.db').as_posix()}"
    monkeypatch.setenv("PERMITPILOT_DATABASE_URL", url)
    get_settings.cache_clear()
    engine = make_engine(url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        # Payload points at an application that does not exist: the reminder is skipped, job done.
        enqueue(s, "reminder", {"application_id": 1, "template": "corrections_due"}, T0)
        s.commit()
    try:
        stats = worker.run_once()
    finally:
        get_settings.cache_clear()
    assert (stats.claimed, stats.done) == (1, 1)
    with factory() as s:
        assert s.query(Job).one().status == "done"
    engine.dispose()


def test_worker_id_is_host_and_pid() -> None:
    host, pid = worker.worker_id().rsplit(":", 1)
    assert host and pid.isdigit()

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db import Base, init_db, make_engine, make_session_factory


@pytest.fixture
def engine() -> Iterator[Engine]:
    # CI also runs the suite against PostgreSQL by setting TEST_DATABASE_URL.
    url = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    eng = make_engine(url)
    Base.metadata.drop_all(eng)
    init_db(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as s:
        yield s

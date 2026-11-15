import pytest
from sqlalchemy.orm import Session

from app.services import (
    InvalidInput,
    NotFound,
    create_application,
    ingest_ordinance,
    permit_types_for,
    search_chunks,
    set_item_done,
)
from tests.helpers import T0, new_application, seed_ordinance


def test_ingest_stores_chunks_and_requirements(session: Session) -> None:
    ordinance = seed_ordinance(session)
    assert len(ordinance.chunks) == 13
    assert {r.extractor for r in ordinance.requirements} == {"rules"}
    assert permit_types_for(ordinance) == ["deck", "fence", "shed", "roof"]


def test_ingest_rejects_text_without_sections(session: Session) -> None:
    with pytest.raises(InvalidInput):
        ingest_ordinance(session, "X", "Y", "Just a paragraph with no headings.", None)


def test_create_application_validates_inputs(session: Session) -> None:
    ordinance = seed_ordinance(session)
    with pytest.raises(NotFound):
        create_application(session, 999, "deck", "a", "b", "c@d.e", T0)
    with pytest.raises(InvalidInput):
        create_application(session, ordinance.id, "pool", "a", "b", "c@d.e", T0)


def test_set_item_done_toggles_and_locks_when_closed(session: Session) -> None:
    app = new_application(session)
    item = app.checklist[0]
    set_item_done(session, app.id, item.id, True, T0)
    assert item.done and item.done_at == T0
    set_item_done(session, app.id, item.id, False, T0)
    assert not item.done and item.done_at is None

    with pytest.raises(NotFound):
        set_item_done(session, app.id + 1, item.id, True, T0)

    app.state = "closed"
    with pytest.raises(InvalidInput):
        set_item_done(session, app.id, item.id, True, T0)


def test_search_returns_cited_excerpts(session: Session) -> None:
    seed_ordinance(session)
    hits = search_chunks(session, "fence height front yard")
    assert hits[0].section == "14-301"
    assert hits[0].excerpt[0] == "Fences in a front yard shall not exceed 4 feet in height."
    assert search_chunks(session, "zzz nothing") == []

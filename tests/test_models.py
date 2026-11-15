from sqlalchemy.orm import Session

from app.models import Chunk, Ordinance


def test_ordinance_chunks_are_ordered_and_cascade(session: Session) -> None:
    o = Ordinance(jurisdiction="Example City", title="Code", source_text="x")
    o.chunks = [
        Chunk(position=1, section="2", heading="B", text="b"),
        Chunk(position=0, section="1", heading="A", text="a"),
    ]
    session.add(o)
    session.commit()
    session.refresh(o)
    assert [c.section for c in o.chunks] == ["1", "2"]

    session.delete(o)
    session.commit()
    assert session.query(Chunk).count() == 0

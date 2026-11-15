from pathlib import Path

from app.chunking import chunk_ordinance

SAMPLE = Path(__file__).parents[1] / "app" / "seed" / "lakeport_residential.txt"


def test_splits_on_section_headings() -> None:
    text = "Intro line.\n§ 1-1 First.\nAlpha.\n\n§ 1-2 Second rule.\nBeta\ngamma.\n"
    chunks = chunk_ordinance(text)
    assert [(c.section, c.heading) for c in chunks] == [
        ("preamble", "Preamble"),
        ("1-1", "First"),
        ("1-2", "Second rule"),
    ]
    assert chunks[2].text == "Beta\ngamma."
    assert [c.position for c in chunks] == [0, 1, 2]


def test_accepts_sec_and_section_prefixes() -> None:
    chunks = chunk_ordinance("Sec. 4.10 Fees.\nPay.\nSection 4.11 Refunds\nNone.")
    assert [c.section for c in chunks] == ["4.10", "4.11"]


def test_empty_sections_are_dropped() -> None:
    chunks = chunk_ordinance("§ 1 Reserved.\n\n§ 2 Real.\nText.")
    assert [c.section for c in chunks] == ["2"]


def test_long_sections_split_on_sentence_boundaries() -> None:
    body = " ".join(f"Sentence number {i} shall apply." for i in range(60))
    chunks = chunk_ordinance(f"§ 9 Long.\n{body}", max_chars=300)
    assert len(chunks) > 1
    assert all(len(c.text) <= 300 for c in chunks)
    assert chunks[0].section == "9 (part 1)"
    # Nothing lost or duplicated.
    assert " ".join(c.text for c in chunks) == body


def test_sample_ordinance_has_expected_sections() -> None:
    chunks = chunk_ordinance(SAMPLE.read_text(encoding="utf-8"))
    sections = [c.section for c in chunks]
    assert sections[0] == "preamble"
    assert "14-201" in sections and "14-603" in sections
    assert len(sections) == 13  # preamble + 12 sections

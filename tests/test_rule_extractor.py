from pathlib import Path

import pytest

from app.chunking import chunk_ordinance
from app.extraction import ChunkInput, RuleExtractor
from app.extraction.base import quote_is_grounded
from app.extraction.rules import classify, permit_type_for

SAMPLE = Path(__file__).parents[1] / "app" / "seed" / "lakeport_residential.txt"


def sample_chunks() -> list[ChunkInput]:
    return [
        ChunkInput(i, c.section, c.heading, c.text)
        for i, c in enumerate(chunk_ordinance(SAMPLE.read_text(encoding="utf-8")))
    ]


@pytest.mark.parametrize(
    ("sentence", "category"),
    [
        ("The permit fee for a deck is $150.", "fee"),
        ("A footing inspection is required before concrete is placed.", "inspection"),
        ("Every application shall include a site plan drawn to scale.", "document"),
        ("A contractor shall provide proof of insurance.", "document"),
        ("Revised documents shall be filed within 30 days.", "deadline"),
        ("Footings shall extend at least 48 inches below grade.", "dimension"),
        ("The finished side of a fence shall face the street.", "rule"),
    ],
)
def test_classify(sentence: str, category: str) -> None:
    assert classify(sentence) == category


def test_permit_type_prefers_section_heading() -> None:
    assert permit_type_for("Footings shall extend 48 inches.", "Decks") == "deck"
    assert permit_type_for("The permit fee for a fence is $50.", "Fees") == "fence"
    assert permit_type_for("Every application shall include a site plan.", "Fees") == "*"


def test_sample_ordinance_extraction() -> None:
    chunks = sample_chunks()
    reqs = RuleExtractor().extract(chunks)
    by_chunk = {c.key: c for c in chunks}

    # Every quote must come from the chunk it is attributed to.
    for r in reqs:
        assert quote_is_grounded(r.quote, by_chunk[r.chunk_key].text), r.quote

    deck = [r for r in reqs if r.permit_type == "deck"]
    assert {r.category for r in deck} >= {"document", "fee", "inspection", "dimension"}
    assert any(r.description == "Pay permit fee ($150)" for r in deck)
    assert sum(1 for r in deck if r.category == "inspection") == 2

    general_docs = [r for r in reqs if r.permit_type == "*" and r.category == "document"]
    assert len(general_docs) == 3  # owner authorization, site plan, insurance

    # Scope statements and exemptions are not requirements.
    assert not any("exempt" in r.quote for r in reqs)
    assert not any("Chapter 15" in r.quote for r in reqs)
    # The preamble disclaimer is never a requirement.
    assert not any(by_chunk[r.chunk_key].section == "preamble" for r in reqs)


def test_non_refundable_fee_sentence_is_kept_as_rule_not_fee_amount() -> None:
    chunk = ChunkInput(0, "1", "Fees", "Fees shall be paid before the permit is issued.")
    [req] = RuleExtractor().extract([chunk])
    assert req.category == "rule"


def test_type_keywords_match_whole_words_only() -> None:
    # "proof" contains "roof"; it must not make an insurance rule roof-only.
    assert permit_type_for("A contractor shall provide proof of insurance.", "Contents") == "*"


def test_inspection_division_is_not_an_inspection() -> None:
    sentence = "The Building Inspection Division shall complete plan review within 10 days."
    assert classify(sentence) == "deadline"


def test_permit_expiration_is_extracted_as_deadline() -> None:
    reqs = RuleExtractor().extract(sample_chunks())
    expiry = [r for r in reqs if "expires" in r.quote]
    assert len(expiry) == 2
    assert {r.category for r in expiry} == {"deadline"}
    # The conditional renewal fee is not a checklist fee.
    assert not any("$25" in r.quote for r in reqs if r.category == "fee")

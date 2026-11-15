import math
from pathlib import Path

import pytest

from app.chunking import chunk_ordinance
from app.retrieval import BM25Index, best_sentences, stem, tokenize

SAMPLE = Path(__file__).parents[1] / "app" / "seed" / "lakeport_residential.txt"


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("decks", "deck"),
        ("fences", "fence"),
        ("inspections", "inspection"),
        ("footings", "footing"),
        ("building", "building"),
        ("properties", "property"),
        ("glass", "glass"),
        ("is", "is"),
        ("boxes", "box"),
    ],
)
def test_stem(word: str, expected: str) -> None:
    assert stem(word) == expected


def test_tokenize_drops_stopwords_and_punctuation() -> None:
    assert tokenize("What are the Deck footings?") == ["deck", "footing"]


def test_bm25_matches_hand_computed_score() -> None:
    # Two docs, query "deck". Only doc a contains it, once, and both docs have length 2.
    index = BM25Index([("a", "deck permit"), ("b", "fence permit")], k1=1.5, b=0.75)
    idf = math.log(1 + (2 - 1 + 0.5) / (1 + 0.5))
    expected = idf * 1 * 2.5 / (1 + 1.5 * 1.0)
    hits = index.search("deck")
    assert [h.key for h in hits] == ["a"]
    assert hits[0].score == pytest.approx(expected)


def test_rare_terms_outweigh_common_terms() -> None:
    index = BM25Index(
        [
            ("common", "permit permit permit"),
            ("rare", "permit guard"),
            ("other", "permit fee"),
        ]
    )
    assert index.search("permit guard")[0].key == "rare"


def test_empty_query_and_empty_index() -> None:
    assert BM25Index([("a", "x")]).search("the of and") == []
    assert BM25Index([]).search("deck") == []


def test_sample_ordinance_retrieval_finds_the_right_sections() -> None:
    chunks = chunk_ordinance(SAMPLE.read_text(encoding="utf-8"))
    index = BM25Index([(c.section, f"{c.heading} {c.text}") for c in chunks])
    assert index.search("how far below grade must deck footings extend")[0].key == "14-201"
    assert index.search("maximum fence height in front yard")[0].key == "14-301"
    assert index.search("when does my permit expire")[0].key == "14-602"


def test_best_sentences_returns_matching_sentences_in_document_order() -> None:
    text = "Fences shall face out. Decks need footings. Deck guards are 36 inches."
    assert best_sentences("deck guard", text) == [
        "Decks need footings.",
        "Deck guards are 36 inches.",
    ]
    assert best_sentences("roof", text) == []


def test_best_sentences_prefers_denser_sentence_on_ties() -> None:
    chunks = chunk_ordinance(SAMPLE.read_text(encoding="utf-8"))
    decks = next(c for c in chunks if c.section == "14-201")
    first = best_sentences("how deep must deck footings go", decks.text, limit=1)
    assert first == ["Footings for decks shall extend at least 48 inches below finished grade."]

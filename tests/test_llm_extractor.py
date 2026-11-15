from types import SimpleNamespace
from typing import Any

import pytest

from app.extraction.base import ChunkInput
from app.extraction.llm import (
    LLMExtraction,
    LLMExtractor,
    LLMRequirement,
    build_llm_extractor,
    render_prompt,
)
from app.extraction.pipeline import run_extraction

CHUNKS = [
    ChunkInput(0, "preamble", "Preamble", "Sample code."),
    ChunkInput(
        1,
        "14-201",
        "Decks",
        "Footings for decks shall extend at least 48 inches below finished grade. "
        "A deck shall be set back at least 5 feet from any side lot line.",
    ),
    ChunkInput(2, "14-104", "Fees", "The permit fee for a deck is $150."),
]


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, messages: FakeMessages) -> None:
        self.messages = messages


def req(section: str, quote: str, **kw: Any) -> LLMRequirement:
    defaults: dict[str, Any] = {
        "permit_type": "deck",
        "category": "dimension",
        "description": "Do the thing",
    }
    defaults.update(kw)
    return LLMRequirement(section=section, quote=quote, **defaults)


def response_with(*items: LLMRequirement, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(parsed_output=LLMExtraction(requirements=list(items)), stop_reason=stop_reason)


def test_grounded_items_are_kept_and_ungrounded_are_rejected() -> None:
    messages = FakeMessages(
        response_with(
            req("14-201", "Footings for decks shall extend at least 48 inches below finished grade."),
            # Paraphrase, not verbatim: rejected.
            req("14-201", "Deck footings must be 4 feet deep."),
            # Section that does not exist: rejected.
            req("99-999", "The permit fee for a deck is $150."),
            req("14-104", "the permit fee for a deck is $150.", category="fee"),
        )
    )
    extractor = LLMExtractor(FakeClient(messages), "test-model")
    out = extractor.extract(CHUNKS)

    assert [(r.chunk_key, r.category) for r in out] == [(1, "dimension"), (2, "fee")]
    assert len(extractor.rejected) == 2


def test_request_uses_structured_output_and_skips_preamble() -> None:
    messages = FakeMessages(response_with())
    LLMExtractor(FakeClient(messages), "test-model").extract(CHUNKS)
    call = messages.calls[0]
    assert call["output_format"] is LLMExtraction
    assert call["model"] == "test-model"
    prompt = call["messages"][0]["content"]
    assert "[14-201] Decks" in prompt
    assert "Sample code." not in prompt


def test_duplicate_quotes_are_collapsed() -> None:
    quote = "The permit fee for a deck is $150."
    messages = FakeMessages(response_with(req("14-104", quote), req("14-104", quote)))
    assert len(LLMExtractor(FakeClient(messages), "m").extract(CHUNKS)) == 1


def test_refusal_raises() -> None:
    messages = FakeMessages(response_with(stop_reason="refusal"))
    with pytest.raises(RuntimeError):
        LLMExtractor(FakeClient(messages), "m").extract(CHUNKS)


def test_pipeline_falls_back_to_rules_on_api_error() -> None:
    messages = FakeMessages(error=ConnectionError("network down"))
    result = run_extraction(CHUNKS, LLMExtractor(FakeClient(messages), "m"))
    assert result.extractor == "rules"
    assert result.fallback_reason == "LLM extraction failed: ConnectionError"
    assert len(result.requirements) == 3


def test_pipeline_falls_back_when_nothing_is_grounded() -> None:
    messages = FakeMessages(response_with(req("14-201", "Invented rule that is not in the text.")))
    result = run_extraction(CHUNKS, LLMExtractor(FakeClient(messages), "m"))
    assert result.extractor == "rules"
    assert result.fallback_reason == "LLM returned no grounded requirements"


def test_pipeline_uses_llm_result_when_grounded() -> None:
    messages = FakeMessages(response_with(req("14-104", "The permit fee for a deck is $150.")))
    result = run_extraction(CHUNKS, LLMExtractor(FakeClient(messages), "m"))
    assert result.extractor == "llm"
    assert result.fallback_reason is None


def test_no_key_means_no_llm_extractor() -> None:
    assert build_llm_extractor(None, "m") is None
    assert build_llm_extractor("", "m") is None


def test_render_prompt_lists_sections_in_order() -> None:
    prompt = render_prompt(CHUNKS)
    assert prompt.index("[14-201]") < prompt.index("[14-104]")

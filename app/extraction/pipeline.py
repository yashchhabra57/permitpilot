"""Choose an extractor and fall back safely."""

import logging
from dataclasses import dataclass

from app.extraction.base import ChunkInput, ExtractedRequirement, Extractor
from app.extraction.rules import RuleExtractor

log = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    requirements: list[ExtractedRequirement]
    extractor: str
    fallback_reason: str | None = None


def run_extraction(chunks: list[ChunkInput], llm: Extractor | None) -> ExtractionResult:
    if llm is not None:
        try:
            reqs = llm.extract(chunks)
            if reqs:
                return ExtractionResult(reqs, llm.name)
            reason = "LLM returned no grounded requirements"
        except Exception as exc:  # any API, network or validation failure
            log.warning("LLM extraction failed, falling back to rules: %s", exc)
            reason = f"LLM extraction failed: {type(exc).__name__}"
        rules = RuleExtractor()
        return ExtractionResult(rules.extract(chunks), rules.name, reason)
    rules = RuleExtractor()
    return ExtractionResult(rules.extract(chunks), rules.name)

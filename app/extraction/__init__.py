"""Turn ordinance chunks into structured requirements.

Two extractors share one output shape:
- RuleExtractor: deterministic sentence rules. Always available, no API key needed.
- LLMExtractor: structured outputs from an LLM, with every item checked against the source text.
"""

from app.extraction.base import (
    CATEGORIES,
    PERMIT_TYPES,
    ChunkInput,
    ExtractedRequirement,
    Extractor,
)
from app.extraction.rules import RuleExtractor

__all__ = [
    "CATEGORIES",
    "PERMIT_TYPES",
    "ChunkInput",
    "ExtractedRequirement",
    "Extractor",
    "RuleExtractor",
]

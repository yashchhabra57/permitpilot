"""Shared types for requirement extractors."""

import re
from dataclasses import dataclass
from typing import Literal, Protocol

PermitType = Literal["deck", "fence", "shed", "roof", "*"]
Category = Literal["document", "fee", "inspection", "dimension", "deadline", "rule"]

PERMIT_TYPES: tuple[str, ...] = ("deck", "fence", "shed", "roof")
CATEGORIES: tuple[str, ...] = ("document", "fee", "inspection", "dimension", "deadline", "rule")


@dataclass(frozen=True)
class ChunkInput:
    key: int
    section: str
    heading: str
    text: str


@dataclass(frozen=True)
class ExtractedRequirement:
    chunk_key: int
    permit_type: str
    category: str
    description: str
    quote: str
    mandatory: bool


class Extractor(Protocol):
    name: str

    def extract(self, chunks: list[ChunkInput]) -> list[ExtractedRequirement]: ...


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_is_grounded(quote: str, source: str) -> bool:
    """True when the quote appears verbatim (ignoring case and whitespace) in the source."""
    q = normalize_ws(quote)
    return len(q) >= 12 and q in normalize_ws(source)

"""LLM requirement extraction with structured outputs and a grounding check.

The model reads every section and returns a typed list of requirements (the SDK validates the
JSON against the Pydantic schema). We then reject anything we cannot verify:

- the cited section must exist in the ordinance, and
- the quote must appear verbatim (ignoring case and whitespace) in that section.

A model can paraphrase or invent a rule. Requiring a verbatim quote means every checklist item a
contractor sees can be traced back to the ordinance text. If the API call fails for any reason,
the caller falls back to the rule extractor, so the app never depends on the API being up.
"""

import logging
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.extraction.base import (
    Category,
    ChunkInput,
    ExtractedRequirement,
    PermitType,
    quote_is_grounded,
)

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You extract permit requirements from municipal ordinance text for small \
contractors. Return one item per distinct obligation, fee, required document, required \
inspection, dimensional limit, or deadline that applies to the applicant or their project.

Rules:
- `section` must be the exact section label shown in brackets.
- `quote` must be copied verbatim from that section: one full sentence, no edits.
- `description` is a short imperative checklist line for the contractor, e.g. "Attach a site \
plan drawn to scale".
- `permit_type` is deck, fence, shed or roof when the requirement only applies to that work, \
otherwise "*".
- Skip scope statements, exemptions, and duties that fall only on the city.
"""


class LLMRequirement(BaseModel):
    section: str
    permit_type: PermitType
    category: Category
    description: str = Field(max_length=200)
    quote: str
    mandatory: bool = True


class LLMExtraction(BaseModel):
    requirements: list[LLMRequirement]


class MessagesClient(Protocol):
    """The slice of the Anthropic client we use, so tests can pass a fake."""

    @property
    def messages(self) -> Any: ...


def render_prompt(chunks: list[ChunkInput]) -> str:
    parts = [f"[{c.section}] {c.heading}\n{c.text}" for c in chunks if c.section != "preamble"]
    return "Ordinance sections:\n\n" + "\n\n".join(parts)


class LLMExtractor:
    name = "llm"

    def __init__(self, client: MessagesClient, model: str) -> None:
        self.client = client
        self.model = model
        self.rejected: list[LLMRequirement] = []

    def extract(self, chunks: list[ChunkInput]) -> list[ExtractedRequirement]:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": render_prompt(chunks)}],
            output_format=LLMExtraction,
        )
        if getattr(response, "stop_reason", None) == "refusal":
            raise RuntimeError("model declined the extraction request")
        parsed: LLMExtraction | None = getattr(response, "parsed_output", None)
        if parsed is None:
            raise RuntimeError("model returned no structured output")
        return self.ground(parsed, chunks)

    def ground(self, parsed: LLMExtraction, chunks: list[ChunkInput]) -> list[ExtractedRequirement]:
        by_section = {c.section: c for c in chunks}
        out: list[ExtractedRequirement] = []
        seen: set[tuple[int, str]] = set()
        self.rejected = []
        for item in parsed.requirements:
            chunk = by_section.get(item.section)
            if chunk is None or not quote_is_grounded(item.quote, chunk.text):
                self.rejected.append(item)
                continue
            key = (chunk.key, item.quote.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ExtractedRequirement(
                    chunk_key=chunk.key,
                    permit_type=item.permit_type,
                    category=item.category,
                    description=item.description.strip(),
                    quote=item.quote.strip(),
                    mandatory=item.mandatory,
                )
            )
        if self.rejected:
            log.warning("dropped %d ungrounded LLM requirements", len(self.rejected))
        return out


def build_llm_extractor(api_key: str | None, model: str) -> LLMExtractor | None:
    """Return an LLM extractor when a key and the SDK are available, else None."""
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        log.info("anthropic SDK not installed; using rule extractor")
        return None
    return LLMExtractor(anthropic.Anthropic(api_key=api_key), model)

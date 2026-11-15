"""Deterministic requirement extraction.

Legal drafting is formulaic: obligations use "shall", "must" or "is required", and fees are
stated as "The permit fee for X is $N". We split each section into sentences, keep the ones that
state an obligation or a fee, then classify them with keyword rules.
"""

import re

from app.extraction.base import ChunkInput, ExtractedRequirement

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_OBLIGATION_RE = re.compile(r"\b(shall|must|is required|are required|expires)\b", re.I)
_NEGATED_SCOPE_RE = re.compile(r"\b(is|are) exempt\b|\bgoverned by\b", re.I)
# Only base permit fees. "Renewed for a fee of $25" is conditional, not a checklist item.
_FEE_RE = re.compile(r"\bpermit fee\b.*\$\s?\d", re.I)
# "Building Inspection Division" names an office; it is not an inspection.
_INSPECTION_RE = re.compile(r"\binspection\b(?!\s+division)", re.I)
_DIMENSION_RE = re.compile(r"\b\d[\d,]*\s*(inch|inches|feet|foot|square feet)\b", re.I)
_DEADLINE_RE = re.compile(r"\bwithin \d+ (business )?days\b|\b\d+ days after\b", re.I)
_DOCUMENT_RE = re.compile(
    r"\bapplication\b.*\b(include|provide|state|show|submit)\b|"
    r"\b(provide|submit)\b.*\b(proof|plan|survey|documents?)\b",
    re.I,
)

# Whole words only: a substring check would find "roof" inside "proof".
_TYPE_PATTERNS: dict[str, re.Pattern[str]] = {
    "deck": re.compile(r"\bdecks?\b", re.I),
    "fence": re.compile(r"\bfences?\b", re.I),
    "shed": re.compile(r"\bsheds?\b", re.I),
    "roof": re.compile(r"\broof(s|ing)?\b", re.I),
}


def _types_mentioned(text: str) -> set[str]:
    return {t for t, pattern in _TYPE_PATTERNS.items() if pattern.search(text)}


def classify(sentence: str) -> str:
    if _FEE_RE.search(sentence):
        return "fee"
    # Deadlines win over inspections and documents: "submit revised documents within 30 days"
    # and "expires if the final inspection is not requested within 365 days" are time limits,
    # not things to check off before submitting.
    if _DEADLINE_RE.search(sentence):
        return "deadline"
    if _INSPECTION_RE.search(sentence):
        return "inspection"
    if _DOCUMENT_RE.search(sentence):
        return "document"
    if _DIMENSION_RE.search(sentence):
        return "dimension"
    return "rule"


def permit_type_for(sentence: str, heading: str) -> str:
    """A section titled 'Decks' scopes all its sentences; otherwise use what the sentence names."""
    from_heading = _types_mentioned(heading)
    if len(from_heading) == 1:
        return from_heading.pop()
    from_sentence = _types_mentioned(sentence)
    if len(from_sentence) == 1:
        return from_sentence.pop()
    return "*"


def _describe(sentence: str, category: str) -> str:
    s = sentence.strip().rstrip(".")
    if category == "fee":
        m = re.search(r"\$\s?[\d,]+(?:\.\d{2})?", s)
        return f"Pay permit fee ({m.group(0).replace(' ', '')})" if m else s
    return s[0].upper() + s[1:]


class RuleExtractor:
    name = "rules"

    def extract(self, chunks: list[ChunkInput]) -> list[ExtractedRequirement]:
        out: list[ExtractedRequirement] = []
        for chunk in chunks:
            if chunk.section == "preamble":
                continue
            for sentence in _SENTENCE_RE.split(chunk.text.replace("\n", " ")):
                sentence = sentence.strip()
                if not sentence:
                    continue
                if not (_FEE_RE.search(sentence) or _OBLIGATION_RE.search(sentence)):
                    continue
                if _NEGATED_SCOPE_RE.search(sentence):
                    continue
                category = classify(sentence)
                out.append(
                    ExtractedRequirement(
                        chunk_key=chunk.key,
                        permit_type=permit_type_for(sentence, chunk.heading),
                        category=category,
                        description=_describe(sentence, category),
                        quote=sentence,
                        mandatory=True,
                    )
                )
        return out

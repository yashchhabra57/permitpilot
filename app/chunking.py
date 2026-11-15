"""Split ordinance text into citable sections.

Municipal codes are organized as numbered sections ("§ 14-201 Decks."). Each section becomes a
chunk, which is both the unit we search over and the unit we cite. Very long sections are split
on paragraph boundaries so one chunk never dominates retrieval.
"""

import re
from dataclasses import dataclass

SECTION_RE = re.compile(r"^\s*(?:§|Sec\.|Section)\s*([0-9][0-9A-Za-z.\-]*)\s+(.+?)\s*$")


@dataclass(frozen=True)
class ChunkDraft:
    position: int
    section: str
    heading: str
    text: str


def _normalize(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def _split_long(body: str, max_chars: int) -> list[str]:
    if len(body) <= max_chars:
        return [body]
    parts: list[str] = []
    current = ""
    # Prefer paragraph breaks, then sentence breaks.
    pieces = re.split(r"\n\s*\n", body)
    if len(pieces) == 1:
        pieces = re.split(r"(?<=[.;])\s+", body)
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if current and len(current) + 1 + len(piece) > max_chars:
            parts.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        parts.append(current)
    return parts


def chunk_ordinance(text: str, max_chars: int = 1200) -> list[ChunkDraft]:
    sections: list[tuple[str, str, list[str]]] = []
    preamble: list[str] = []
    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            heading = m.group(2).rstrip(".")
            sections.append((m.group(1), heading, []))
        elif sections:
            sections[-1][2].append(line)
        else:
            preamble.append(line)

    drafts: list[ChunkDraft] = []
    pre = _normalize("\n".join(preamble))
    if pre:
        drafts.append(ChunkDraft(0, "preamble", "Preamble", pre))

    for section, heading, lines in sections:
        body = _normalize("\n".join(lines))
        if not body:
            continue
        parts = _split_long(body, max_chars)
        for i, part in enumerate(parts):
            label = section if len(parts) == 1 else f"{section} (part {i + 1})"
            drafts.append(ChunkDraft(len(drafts), label, heading, part))
    return drafts

"""Split a page's sections into chunks for embedding.

A chunk packs whole paragraphs up to MAX_CHARS (~350 tokens). Each chunk names its section
("## Pricing") so it makes sense on its own, and when a section continues into the next chunk,
that chunk repeats the tail of the previous one (OVERLAP_CHARS) so a fact cut at the boundary is
still found. Paragraphs longer than PIECE_CHARS are cut at sentence, then word, boundaries.

Worst case per chunk: heading (HEADING_CHARS) + overlap + one piece <= MAX_CHARS.
"""
import hashlib
import re
from dataclasses import dataclass

from extract import Section

MAX_CHARS = 1500
PIECE_CHARS = 1000
OVERLAP_CHARS = 200
HEADING_CHARS = 200
MAX_CHUNKS_PER_PAGE = 60        # bounds the embedding cost of one huge page

_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")


@dataclass(frozen=True)
class Chunk:
    ord: int
    heading: str       # section of the chunk's first paragraph ("" if none)
    content: str       # what the model sees as context

    def embed_text(self, page_title: str) -> str:
        """What gets embedded: the page title adds context the chunk itself may not repeat."""
        return f"{page_title}\n{self.content}" if page_title else self.content


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _split_long(text: str) -> list[str]:
    """Cut text into pieces of at most PIECE_CHARS, preferring sentence then word boundaries."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        while len(sentence) > PIECE_CHARS:
            cut = sentence.rfind(" ", 0, PIECE_CHARS)
            cut = cut if cut > PIECE_CHARS // 2 else PIECE_CHARS
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > PIECE_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def _paragraphs(text: str) -> list[str]:
    out: list[str] = []
    for paragraph in text.split("\n"):
        out.extend([paragraph] if len(paragraph) <= PIECE_CHARS else _split_long(paragraph))
    return [p for p in out if p.strip()]


def _tail(text: str) -> str:
    """The last ~OVERLAP_CHARS of text, starting at a word boundary."""
    if len(text) <= OVERLAP_CHARS:
        return text
    tail = text[-OVERLAP_CHARS:]
    space = tail.find(" ")
    return tail[space + 1:] if space != -1 else tail


def chunk_sections(sections: list[Section]) -> list[Chunk]:
    chunks: list[Chunk] = []
    lines: list[str] = []          # the chunk being built
    first_heading = ""
    last_heading: str | None = None

    def emit() -> None:
        content = "\n".join(lines).strip()
        if content:
            chunks.append(Chunk(len(chunks), first_heading, content))

    for section in sections:
        heading = section.heading[:HEADING_CHARS]
        for paragraph in _paragraphs(section.text):
            same_section = heading == last_heading
            addition = [paragraph] if same_section or not heading else [f"## {heading}", paragraph]
            if lines and len("\n".join(lines + addition)) > MAX_CHARS:
                previous = "\n".join(lines)
                emit()
                if len(chunks) >= MAX_CHUNKS_PER_PAGE:
                    return chunks
                # A new chunk always names its section; mid-section it also repeats the overlap.
                lines = ([f"## {heading}"] if heading else []) + ([_tail(previous)] if same_section else [])
                first_heading = heading
                addition = [paragraph]
            elif not lines:
                first_heading = heading
            lines.extend(addition)
            last_heading = heading
    emit()
    return chunks[:MAX_CHUNKS_PER_PAGE]

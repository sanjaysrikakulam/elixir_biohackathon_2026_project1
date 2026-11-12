"""Apply whole-line edits to the original text of a document.

Edits are applied from the bottom of the file upwards so earlier line numbers stay valid. Line endings
(LF or CRLF) and the presence of a final newline are preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .doc import Doc, Span


class EditError(ValueError):
    pass


@dataclass(frozen=True)
class Edit:
    kind: Literal["insert", "replace", "delete"]
    start: int
    end: int
    lines: list[str] = field(default_factory=list)


def apply(text: str, edits: list[Edit]) -> str:
    eol = "\r\n" if "\r\n" in text else "\n"
    if eol == "\r\n" and text.count("\n") != text.count("\r\n"):
        raise EditError("the file has mixed line endings (LF and CRLF); normalise it first")
    lines = text.split(eol)
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.start < previous.end:
            raise EditError(f"overlapping edits at lines {previous.start + 1}-{current.end}")
    for edit in reversed(ordered):
        if edit.kind == "insert":
            lines[edit.start : edit.start] = edit.lines
        elif edit.kind == "replace":
            lines[edit.start : edit.end] = edit.lines
        elif edit.kind == "delete":
            lines[edit.start : edit.end] = []
        else:
            raise EditError(f"unknown edit kind {edit.kind!r}")
    return eol.join(lines)


def _separated_by_blank_lines(doc: Doc, spans: list[Span]) -> bool:
    """Whether most neighbouring entries in a collection have a blank line between them."""
    gaps = list(zip(spans, spans[1:], strict=False))
    if not gaps:
        return False
    blank = sum(any(not doc.lines[i].strip() for i in range(a.end, b.start)) for a, b in gaps)
    return blank * 2 > len(gaps)


def insert_after_edit(doc: Doc, path: list, *, anchor: str, lines: list[str], field: str | None = None) -> Edit:
    """Insert ``lines`` after the last child of ``path`` matching ``anchor`` (else after the last child)."""
    if doc.is_flow(path):
        raise EditError(f"{doc.name}: '{'.'.join(map(str, path))}' is a flow collection; cannot add lines to it")
    children = doc.children(path)
    if not children:
        raise EditError(f"{doc.name}: '{'.'.join(map(str, path))}' is empty; add the first entry by hand")
    matches = doc.matching_children(path, anchor, field)
    target = (matches or children)[-1][1]
    spans = [span for _, span in children]
    prefix = [""] if _separated_by_blank_lines(doc, spans) else []
    return Edit("insert", target.end, target.end, prefix + lines)


def delete_edit(doc: Doc, span: Span) -> Edit:
    """Remove an entry. A blank separator line in front of it goes too when the entry is followed by a blank
    line or the end of the file, which exactly undoes :func:`insert_after_edit`."""
    start = span.start
    before_blank = start > 0 and not doc.lines[start - 1].strip()
    after_blank = span.end >= len(doc.lines) or not doc.lines[span.end].strip()
    if before_blank and after_blank:
        start -= 1
    return Edit("delete", start, span.end, [])

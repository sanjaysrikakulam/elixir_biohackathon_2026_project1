"""Find where entries live in a YAML (or masked Jinja) document, by line.

ruamel.yaml reports where each key or list item *starts*. Where it ends is derived from indentation:
an entry owns every following line that is indented deeper than the entry itself (comments included, so a
block scalar ending in ``# ...`` or a commented-out line inside a block stays with it). Blank lines and
comment lines at the entry's own indentation or shallower only belong to it when a deeper line follows;
trailing ones belong to whatever comes next (for example a section banner).
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar
from ruamel.yaml.error import YAMLError

from .mask import mask


class LocateError(LookupError):
    pass


@dataclass(frozen=True)
class Span:
    """Lines ``start`` (inclusive) to ``end`` (exclusive), 0-based; ``indent`` is the entry's column."""

    start: int
    end: int
    indent: int


def plain(value: Any) -> Any:
    """ruamel's round-trip types as plain dicts, lists and scalars."""
    if isinstance(value, TaggedScalar):
        # e.g. an inline "!vault |" value: compare by tag and text, not by object identity
        return ("!tag", str(getattr(value.tag, "value", value.tag)), str(value.value))
    if isinstance(value, dict):
        return {plain(k): plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value


def parse(text: str) -> Any:
    yaml = YAML(typ="rt")
    yaml.allow_duplicate_keys = False
    return yaml.load(mask(text))


@functools.lru_cache(maxsize=256)
def _parsed(text: str) -> tuple[Any, Any]:
    """Parsed root and its plain data. Cached: planning re-reads the same files many times. Callers must treat
    both as read-only (everything that edits data works on copies)."""
    root = parse(text)
    return root, plain(root)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


class Doc:
    def __init__(self, text: str, name: str = "<document>", *, cache: bool = True):
        """``cache=False`` for secret content (decrypted vaults), which must not stay in memory."""
        self.text = text
        self.name = name
        self.lines = [line.rstrip("\r") for line in text.split("\n")]
        try:
            if cache:
                self.root, self.data = _parsed(text)
            else:
                self.root = parse(text)
                self.data = plain(self.root)
        except (YAMLError, ValueError) as exc:
            raise LocateError(f"{name}: cannot parse as YAML: {exc}") from exc

    # -- navigation ------------------------------------------------------------------------------------------

    def node(self, path: list[Any]) -> Any:
        node = self.root
        for i, part in enumerate(path):
            try:
                node = node[part]
            except (KeyError, IndexError, TypeError):
                where = ".".join(str(p) for p in path[: i + 1])
                raise LocateError(f"{self.name}: no '{where}' in the document") from None
        return node

    def _collection(self, path: list[Any], kind: type) -> Any:
        node = self.node(path)
        if not isinstance(node, kind):
            what = "mapping" if kind is CommentedMap else "list"
            raise LocateError(f"{self.name}: '{'.'.join(map(str, path)) or '<root>'}' is not a {what}")
        return node

    # -- spans -----------------------------------------------------------------------------------------------

    def _span(self, start: int, indent: int, compact_seq: bool = False) -> Span:
        last = start
        i = start + 1
        while i < len(self.lines):
            line = self.lines[i]
            stripped = line.strip()
            if stripped:
                ind = _indent(line)
                if ind > indent or (compact_seq and ind == indent and stripped.startswith("- ")):
                    last = i
                elif not stripped.startswith("#"):
                    break
            i += 1
        return Span(start, last + 1, indent)

    def _key_span(self, mapping: CommentedMap, key: Any) -> Span:
        line, col = mapping.lc.key(key)
        return self._span(line, col, compact_seq=isinstance(mapping[key], CommentedSeq))

    def _item_span(self, seq: CommentedSeq, index: int) -> Span:
        line, col = seq.lc.item(index)
        dash = self.lines[line].rfind("-", 0, col + 1)
        return self._span(line, dash if dash >= 0 else col)

    def span_of(self, path: list[Any]) -> Span:
        """Span of the entry at ``path`` (a mapping key or a list index)."""
        parent = self.node(path[:-1])
        last = path[-1]
        if isinstance(parent, CommentedSeq) and isinstance(last, int):
            return self._item_span(parent, last)
        if isinstance(parent, CommentedMap):
            return self._key_span(parent, last)
        raise LocateError(f"{self.name}: no entry at '{'.'.join(map(str, path))}'")

    def children(self, path: list[Any]) -> list[tuple[Any, Span]]:
        node = self.node(path)
        if isinstance(node, CommentedMap):
            return [(key, self._key_span(node, key)) for key in node]
        if isinstance(node, CommentedSeq):
            return [(index, self._item_span(node, index)) for index in range(len(node))]
        raise LocateError(f"{self.name}: '{'.'.join(map(str, path))}' is not a mapping or list")

    def map_child(self, path: list[Any], key: Any) -> Span | None:
        mapping = self._collection(path, CommentedMap)
        return self._key_span(mapping, key) if key in mapping else None

    def seq_item(self, path: list[Any], field: str, value: Any) -> Span | None:
        seq = self._collection(path, CommentedSeq)
        for index, item in enumerate(seq):
            if isinstance(item, dict) and item.get(field) == value:
                return self._item_span(seq, index)
        return None

    def is_flow(self, path: list[Any]) -> bool:
        node = self.node(path)
        # ruamel reports True for "{a: 1}", None for an empty "{}" and False for block style.
        return getattr(node, "fa", None) is not None and node.fa.flow_style() is not False

    def matching_children(self, path: list[Any], pattern: str, field: str | None = None) -> list[tuple[Any, Span]]:
        """Children whose key (or, for list items, ``item[field]``) matches the regular expression."""
        node = self.node(path)
        regex = re.compile(pattern)
        result = []
        for key, span in self.children(path):
            label = node[key].get(field) if field and isinstance(node[key], dict) else key
            if isinstance(label, str) and regex.search(label):
                result.append((key, span))
        return result

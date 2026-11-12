"""Hide Jinja2 markup so a template parses as YAML, without changing the number of lines.

* ``{{ expr }}`` becomes ``__J<hash>__``. The token is derived from the expression, so a changed expression
  produces a different parse result and is caught by verification.
* ``{# comment #}`` is removed.
* ``{% macro %}`` ... ``{% endmacro %}`` (and ``raw``/``call``/``filter`` blocks) are commented out line by
  line: their bodies are template text, not YAML.
* A line holding only ``{% ... %}`` tags (``for``, ``if``, ``set`` ...) is commented out; tags inside a line
  with other content are removed.

Expressions and tags that span several lines outside those blocks are not supported and raise
:class:`MaskError` rather than being guessed at.
"""

from __future__ import annotations

import hashlib
import re

_EXPR = re.compile(r"\{\{.*?\}\}")
_COMMENT = re.compile(r"\{#.*?#\}")
_TAG = re.compile(r"\{%.*?%\}")
_BLOCK = re.compile(
    r"\{%-?\s*(macro|raw|call|filter)\b.*?%\}.*?\{%-?\s*end\1\s*-?%\}",
    re.DOTALL,
)


class MaskError(ValueError):
    pass


def token(expression: str) -> str:
    return "__J" + hashlib.sha1(expression.encode()).hexdigest()[:12] + "__"


def _block_lines(text: str) -> set[int]:
    lines: set[int] = set()
    for match in _BLOCK.finditer(text):
        first = text.count("\n", 0, match.start())
        last = text.count("\n", 0, match.end())
        lines.update(range(first, last + 1))
    return lines


def mask(text: str) -> str:
    if "{" not in text:
        return text
    lines = text.split("\n")
    commented = _block_lines(text)
    out = []
    for number, line in enumerate(lines):
        if number in commented:
            out.append("#" + line)
            continue
        line = _COMMENT.sub("", line)
        body = line.rstrip("\r")
        if _TAG.search(body) and not _TAG.sub("", body).strip():
            out.append("#" + line)
            continue
        line = _TAG.sub("", line)
        line = _EXPR.sub(lambda m: token(m.group(0)), line)
        if "{{" in line or "{%" in line or "{#" in line:
            raise MaskError(f"line {number + 1}: Jinja markup spanning several lines is not supported")
        out.append(line)
    return "\n".join(out)

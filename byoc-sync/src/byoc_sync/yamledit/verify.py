"""Check that an edited document differs from the original exactly as intended.

``expected`` receives the parsed (masked) original and returns what the parsed result must be. Anything else
that changed, including a modified Jinja expression, fails the check.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .doc import Doc, LocateError


class VerificationError(AssertionError):
    pass


def _differences(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b), key=str):
            where = f"{path}.{key}" if path else str(key)
            if key not in a:
                out.append(f"+{where}")
            elif key not in b:
                out.append(f"-{where}")
            else:
                out.extend(_differences(a[key], b[key], where))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b, strict=True)) for d in _differences(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"~{path or '<root>'}"]


def verify(before: str, after: str, expected: Callable[[Any], Any], name: str = "<document>") -> None:
    try:
        old = Doc(before, name).data
        new = Doc(after, name).data
    except LocateError as exc:
        raise VerificationError(f"{name}: cannot parse the edited document: {exc}") from exc
    want = expected(old)
    if new != want:
        diff = ", ".join(_differences(want, new)[:10])
        raise VerificationError(f"{name}: unexpected changes after editing: {diff}")

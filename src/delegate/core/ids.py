"""Task references (``DLG-42``) and project keys."""

from __future__ import annotations

import re
from collections.abc import Container

from delegate.core.errors import ValidationError

_REF_RE = re.compile(r"^\s*(?:([A-Za-z][A-Za-z0-9]*)-)?(\d+)\s*$")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")


def format_ref(key: str, seq: int) -> str:
    return f"{key}-{seq}"


def parse_ref(ref: str | int) -> tuple[str | None, int]:
    """Parse ``DLG-42``, ``dlg-42``, ``42`` or ``#42`` into ``(KEY or None, seq)``."""
    text = str(ref).strip().lstrip("#")
    match = _REF_RE.match(text)
    if not match:
        raise ValidationError(f"Invalid task reference {ref!r}; expected e.g. DLG-42 or 42")
    key, seq = match.group(1), int(match.group(2))
    return (key.upper() if key else None), seq


def validate_key(key: str) -> str:
    key = key.strip().upper()
    if not _KEY_RE.match(key):
        raise ValidationError(
            f"Invalid project key {key!r}; use 2-10 letters/digits starting with a letter"
        )
    return key


def derive_key(name: str, taken: Container[str]) -> str:
    """Make a short unique key from a project name, e.g. ``delegate-ai`` -> ``DLG``.

    Strategy: first letter plus following consonants, 3 chars; fall back to plain
    letters, then append digits until unique.
    """
    letters = re.sub(r"[^A-Za-z0-9]", "", name).upper()
    if not letters or not letters[0].isalpha():
        letters = "P" + letters
    consonants = letters[0] + "".join(c for c in letters[1:] if c not in "AEIOU")
    base = consonants[:3] if len(consonants) >= 3 else letters[:3]
    base = base.ljust(2, "X")
    if base not in taken:
        return base
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"

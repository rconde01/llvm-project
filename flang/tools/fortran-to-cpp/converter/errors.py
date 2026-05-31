"""Converter error type.

The translator's contract is *faithful or fail*: when it meets a Fortran
construct it cannot translate correctly, it raises :class:`ConversionError`
rather than emitting a ``// TODO`` placeholder and compilable-but-wrong
C++.  A silent placeholder hides the gap inside plausible-looking output;
a hard error surfaces it immediately so the construct gets implemented.
"""

from __future__ import annotations


class ConversionError(Exception):
    """Raised when the converter cannot faithfully translate a construct.

    ``kind`` is the construct (an AST node kind, IR node name, or a short
    phrase like ``"this DATA initializer"``); ``note`` adds detail and
    ``source`` is the offending Fortran text when available.
    """

    def __init__(self, kind: str, *, note: str = "", source: str = "") -> None:
        self.kind = kind
        self.note = note
        self.source = source
        msg = f"cannot translate {kind}"
        if note:
            msg += f" ({note})"
        if source.strip():
            msg += f": {source.strip()}"
        super().__init__(msg)

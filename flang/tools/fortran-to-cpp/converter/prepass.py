"""Source prepass: make legacy files flang can scan.

flang's scanner rejects stray control characters — most commonly a DOS
end-of-file byte (``0x1A`` / ^Z) left in old fixed-form sources — with
"bad character (0x..) in Fortran token", which aborts the whole parse.
Such bytes are never a valid Fortran token, so this prepass replaces
them with spaces before the file is handed to flang.

Note: flang already tolerates *non-ASCII* bytes (e.g. a curly apostrophe
in a comment), so those are intentionally left untouched — only C0
control characters that aren't legitimate source whitespace are
replaced.  The bad byte is often not inside a comment (a lone ^Z sits on
its own line), so the replacement is applied to the whole source rather
than to comment text only.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# C0 control bytes that are legitimate Fortran source whitespace and must
# be preserved: tab, line feed, form feed, carriage return.
_ALLOWED_CONTROL = frozenset({0x09, 0x0A, 0x0C, 0x0D})


def _is_bad(byte: int) -> bool:
    return byte < 0x20 and byte not in _ALLOWED_CONTROL


def needs_sanitizing(raw: bytes) -> bool:
    return any(_is_bad(b) for b in raw)


def sanitize_bytes(raw: bytes) -> bytes:
    """Replace scanner-hostile control bytes with spaces; leave everything
    else (including non-ASCII bytes) unchanged."""
    return bytes(0x20 if _is_bad(b) else b for b in raw)


@contextmanager
def sanitized_source(path: str | os.PathLike[str]) -> Iterator[Path]:
    """Yield a path flang can scan.

    If ``path`` contains no scanner-hostile control bytes it is yielded
    unchanged.  Otherwise a sanitized copy is written to a temporary file
    (same suffix, so flang still infers fixed/free form and preprocessing)
    and that path is yielded; it is removed on exit.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        yield p
        return
    if not needs_sanitizing(raw):
        yield p
        return
    fd, tmp = tempfile.mkstemp(suffix=p.suffix, prefix=p.stem + "-sanitized-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(sanitize_bytes(raw))
        yield Path(tmp)
    finally:
        os.unlink(tmp)

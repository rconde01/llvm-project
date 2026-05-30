"""``python -m flang_ast`` entry point — runs the annotation CLI."""

from __future__ import annotations

import sys

from .annotate import main

if __name__ == "__main__":
    sys.exit(main())

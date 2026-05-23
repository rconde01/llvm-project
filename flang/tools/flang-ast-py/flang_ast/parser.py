"""JSON → Node tree conversion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, IO

from .nodes import Node


def parse_json(data: dict[str, Any]) -> Node:
    """Convert an already-decoded JSON dict into a ``Node`` tree."""
    return Node.from_json(data)


def parse_json_string(text: str) -> Node:
    """Parse a JSON string emitted by ``-fdebug-dump-parse-tree-json``."""
    if not text.strip():
        raise ValueError("input JSON is empty")
    return Node.from_json(json.loads(text))


def parse_json_file(path: str | Path | IO[str]) -> Node:
    """Parse a JSON document from a file path or open text stream."""
    if hasattr(path, "read"):
        stream: IO[str] = path  # type: ignore[assignment]
        return parse_json_string(stream.read())
    return parse_json_string(Path(path).read_text(encoding="utf-8"))

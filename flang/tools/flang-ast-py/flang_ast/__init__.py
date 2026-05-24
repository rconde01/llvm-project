"""Typed parser for flang's JSON parse tree dump.

This package converts the JSON document emitted by

    flang -fc1 -fdebug-dump-parse-tree-json[-no-sema] <file.f90>

into a tree of strongly-typed Python data structures, with helpers for
traversal and source-range queries.

Quick start
-----------
>>> from flang_ast import parse_fortran_file
>>> program = parse_fortran_file("hello.f90")
>>> for assign in program.find_all("AssignmentStmt"):
...     print(assign.source.line, assign.fortran)
"""

from __future__ import annotations

from .annotate import (
    CommentAnnotator,
    annotate_tree,
    extract_comments,
    render_report,
)
from .nodes import (
    Comment,
    Node,
    NodeKind,
    SourceRange,
)
from .parser import (
    parse_json,
    parse_json_file,
    parse_json_string,
)
from .runner import (
    FlangError,
    parse_fortran_file,
    parse_fortran_source,
)
from .visitor import NodeTransformer, NodeVisitor, walk

__all__ = [
    "Comment",
    "CommentAnnotator",
    "FlangError",
    "Node",
    "NodeKind",
    "NodeTransformer",
    "NodeVisitor",
    "SourceRange",
    "annotate_tree",
    "extract_comments",
    "parse_fortran_file",
    "parse_fortran_source",
    "parse_json",
    "parse_json_file",
    "parse_json_string",
    "render_report",
    "walk",
]

__version__ = "0.1.0"

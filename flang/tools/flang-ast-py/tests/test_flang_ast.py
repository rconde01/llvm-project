"""End-to-end smoke tests for the flang-ast package.

These tests cover:
  * JSON-string parsing (no external dependencies)
  * Driving an installed flang binary (skipped when not on PATH)
"""

from __future__ import annotations

import json
import os
import shutil
import unittest

from flang_ast import (
    Node,
    NodeKind,
    NodeVisitor,
    SourceRange,
    parse_fortran_source,
    parse_json,
    parse_json_string,
)


# A minimal hand-written sample mimicking the JSON dumper's output.
_SAMPLE_JSON = json.dumps(
    {
        "kind": "Program",
        "children": [
            {
                "kind": "ProgramUnit",
                "children": [
                    {
                        "kind": "MainProgram",
                        "children": [
                            {
                                "kind": "Statement",
                                "source": {
                                    "text": "program demo",
                                    "file": "demo.f90",
                                    "line": 1,
                                    "col": 1,
                                    "endLine": 1,
                                    "endCol": 13,
                                },
                                "children": [
                                    {
                                        "kind": "ProgramStmt",
                                        "children": [
                                            {
                                                "kind": "Name",
                                                "source": {
                                                    "text": "demo",
                                                    "file": "demo.f90",
                                                    "line": 1,
                                                    "col": 9,
                                                    "endLine": 1,
                                                    "endCol": 13,
                                                },
                                                "fortran": "demo",
                                            }
                                        ],
                                    }
                                ],
                            },
                            {
                                "kind": "Statement",
                                "label": 100,
                                "source": {
                                    "text": "x = 1",
                                    "file": "demo.f90",
                                    "line": 2,
                                    "col": 3,
                                    "endLine": 2,
                                    "endCol": 8,
                                },
                                "children": [
                                    {
                                        "kind": "AssignmentStmt",
                                        "fortran": "x=1_4",
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ],
    }
)


class JsonParsingTests(unittest.TestCase):
    def test_string_round_trip(self) -> None:
        root = parse_json_string(_SAMPLE_JSON)
        self.assertIsInstance(root, Node)
        self.assertEqual(root.kind, "Program")
        self.assertEqual(len(root.children), 1)

    def test_parse_json_dict(self) -> None:
        root = parse_json(json.loads(_SAMPLE_JSON))
        self.assertEqual(root.kind, NodeKind.Program)

    def test_source_range_fields(self) -> None:
        root = parse_json_string(_SAMPLE_JSON)
        name = root.find_first(NodeKind.Name)
        self.assertIsNotNone(name)
        assert name is not None  # for the type checker
        self.assertIsInstance(name.source, SourceRange)
        assert name.source is not None
        self.assertEqual(name.source.file, "demo.f90")
        self.assertEqual(name.source.line, 1)
        self.assertEqual(name.source.col, 9)
        self.assertEqual(name.source.end_col, 13)
        self.assertEqual(name.source.text, "demo")
        self.assertEqual(name.fortran, "demo")

    def test_label_is_captured(self) -> None:
        root = parse_json_string(_SAMPLE_JSON)
        labels = [n.label for n in root.find_all("Statement") if n.label is not None]
        self.assertEqual(labels, [100])

    def test_invalid_json_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_json_string("")
        with self.assertRaises(ValueError):
            parse_json_string(json.dumps({"no_kind": True}))


class NavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = parse_json_string(_SAMPLE_JSON)

    def test_find_all(self) -> None:
        kinds = {n.kind for n in self.root.walk()}
        self.assertIn("ProgramStmt", kinds)
        self.assertIn("AssignmentStmt", kinds)

    def test_children_of_kind(self) -> None:
        main = self.root.find_first(NodeKind.MainProgram)
        assert main is not None
        stmts = main.children_of_kind("Statement")
        self.assertEqual(len(stmts), 2)

    def test_require_child_raises(self) -> None:
        with self.assertRaises(LookupError):
            self.root.require_child("NoSuchKind")

    def test_count(self) -> None:
        self.assertEqual(self.root.count("Statement"), 2)
        self.assertEqual(self.root.count("Name"), 1)


class VisitorTests(unittest.TestCase):
    def test_dispatch(self) -> None:
        class Counter(NodeVisitor[None]):
            def __init__(self) -> None:
                self.assignments = 0
                self.names: list[str] = []

            def visit_AssignmentStmt(self, node: Node) -> None:
                self.assignments += 1
                self.generic_visit(node)

            def visit_Name(self, node: Node) -> None:
                if node.fortran:
                    self.names.append(node.fortran)
                self.generic_visit(node)

        root = parse_json_string(_SAMPLE_JSON)
        v = Counter()
        v.visit(root)
        self.assertEqual(v.assignments, 1)
        self.assertEqual(v.names, ["demo"])


@unittest.skipUnless(
    os.environ.get("FLANG") or shutil.which("flang-new") or shutil.which("flang"),
    "no flang binary on PATH (set FLANG to enable this test)",
)
class FlangIntegrationTests(unittest.TestCase):
    SOURCE = "program demo\n  integer :: x\n  x = 1 + 2\nend program\n"

    def test_round_trip_through_flang(self) -> None:
        root = parse_fortran_source(self.SOURCE)
        self.assertEqual(root.kind, "Program")

        main = root.find_first(NodeKind.MainProgram)
        self.assertIsNotNone(main)
        assert main is not None

        assigns = list(root.find_all(NodeKind.AssignmentStmt))
        self.assertEqual(len(assigns), 1)
        # Semantic analysis should populate "fortran" on the assignment.
        self.assertTrue(assigns[0].fortran)

        # Every Name in the program should have a source range.
        for name in root.find_all(NodeKind.Name):
            self.assertIsNotNone(name.source)
            assert name.source is not None
            self.assertGreaterEqual(name.source.line or 0, 1)

    def test_no_sema_mode(self) -> None:
        root = parse_fortran_source(self.SOURCE, sema=False)
        self.assertEqual(root.kind, "Program")


@unittest.skipUnless(
    os.environ.get("FLANG") or shutil.which("flang-new") or shutil.which("flang"),
    "no flang binary on PATH (set FLANG to enable this test)",
)
class ExprSemanticsTests(unittest.TestCase):
    """The dumper emits resolved type, rank, expression category and a
    folded scalar integer value on any node that carries an analyzed
    expression (``Expr`` / ``Variable`` / ``DataStmtConstant`` /
    ``AllocateObject`` / ``PointerObject``).  Consumers can read facts off
    the parse tree instead of re-deriving them from node shape."""

    SOURCE = (
        "program p\n"
        "  integer, parameter :: n = 25\n"
        "  integer :: a(n*n), b\n"
        "  b = 1\n"
        "  a(b+1) = a(b) + 2\n"
        "end program\n"
    )

    def test_constant_expressions_carry_folded_value(self) -> None:
        root = parse_fortran_source(self.SOURCE)
        # ``n*n`` folds to the integer 625; every literal/parameter use
        # carries its folded value as well.
        constants = [
            e for e in root.find_all(NodeKind.Expr)
            if e.category == "constant" and e.value is not None
        ]
        values = sorted({int(c.value) for c in constants if c.value is not None})
        # Expected: 1 (the assignment), 2 (the addend), 25 (n), 625 (n*n).
        for expected in (1, 2, 25, 625):
            self.assertIn(expected, values)

    def test_designator_vs_expression_category(self) -> None:
        root = parse_fortran_source(self.SOURCE)
        exprs = list(root.find_all(NodeKind.Expr))
        # ``b`` and ``a(b)`` are variables (assignable designators).
        # ``b + 1`` and ``a(b) + 2`` are expressions (computed values).
        cats = {e.fortran or "": e.category for e in exprs}
        self.assertEqual(cats.get("b"), "variable")
        self.assertEqual(cats.get("b+1_4"), "expression")

    def test_resolved_type_and_rank_on_expression_nodes(self) -> None:
        root = parse_fortran_source(self.SOURCE)
        for e in root.find_all(NodeKind.Expr):
            # Every analyzed Expr should be typed.
            self.assertEqual(e.sym_type, "INTEGER(4)")
            self.assertEqual(e.rank, 0)


if __name__ == "__main__":
    unittest.main()

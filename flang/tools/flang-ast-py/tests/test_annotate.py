"""Tests for the comment-annotation pipeline."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from flang_ast import (
    Comment,
    CommentAnnotator,
    Node,
    annotate_tree,
    extract_comments,
    parse_fortran_file,
)
from flang_ast.annotate import main as cli_main, render_report


_FORTRAN_SOURCE = """! Top-of-file comment
! that spans two lines
program demo
  ! Variable declaration block
  integer :: x  ! the running total
  integer :: y

  ! Initialise and increment.
  x = 0
  x = x + 1  ! step

  ! End of program follows.
end program
"""


def _have_flang() -> bool:
    return bool(
        os.environ.get("FLANG") or shutil.which("flang-new") or shutil.which("flang")
    )


class CommentScannerTests(unittest.TestCase):
    def test_extracts_full_line_and_inline(self) -> None:
        comments = extract_comments(_FORTRAN_SOURCE, "demo.f90")
        # Expected lines: 1, 2, 4, 5 (inline), 8, 10 (inline), 12.
        kinds = [(c.line, c.is_full_line, c.is_directive) for c in comments]
        self.assertEqual(
            kinds,
            [
                (1, True, False),
                (2, True, False),
                (4, True, False),
                (5, False, False),
                (8, True, False),
                (10, False, False),
                (12, True, False),
            ],
        )

    def test_string_literals_arent_treated_as_comments(self) -> None:
        # A "!" inside a string must not start a comment.
        src = 'print *, "hello !world"  ! real comment\n'
        comments = extract_comments(src, "x.f90")
        self.assertEqual(len(comments), 1)
        self.assertFalse(comments[0].is_full_line)
        self.assertIn("real comment", comments[0].text)

    def test_doubled_quote_escapes(self) -> None:
        src = "x = 'it''s ok'  ! tail\n"
        comments = extract_comments(src, "x.f90")
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].text.strip(), "tail")

    def test_directive_classification(self) -> None:
        src = "!$omp parallel\n!DIR$ inline\n! normal\n"
        comments = extract_comments(src, "x.f90")
        self.assertEqual([c.is_directive for c in comments], [True, True, False])

    def test_fixed_form_column_one_comment(self) -> None:
        src = "C this is a comment\n      integer x  ! trailing\n"
        comments = extract_comments(src, "x.f", fixed_form=True)
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0].line, 1)
        self.assertTrue(comments[0].is_full_line)
        self.assertEqual(comments[1].line, 2)
        self.assertFalse(comments[1].is_full_line)


class AnnotationTests(unittest.TestCase):
    """Tests that don't require flang — use a hand-built AST."""

    def _make_tree(self) -> Node:
        # Statement at line 5 ("integer :: x  ! the running total")
        stmt_x = Node(
            kind="Statement",
            source=Node.from_json(
                {
                    "kind": "Statement",
                    "source": {
                        "text": "integer :: x  ! the running total",
                        "file": "demo.f90",
                        "line": 5,
                        "col": 3,
                        "endLine": 5,
                        "endCol": 36,
                    },
                }
            ).source,
        )
        # Statement at line 9 (x = 0)
        stmt_xeq0 = Node.from_json(
            {
                "kind": "Statement",
                "source": {
                    "text": "x = 0",
                    "file": "demo.f90",
                    "line": 9,
                    "col": 3,
                    "endLine": 9,
                    "endCol": 8,
                },
            }
        )
        # Statement at line 10 (x = x + 1   ! step)
        stmt_xinc = Node.from_json(
            {
                "kind": "Statement",
                "source": {
                    "text": "x = x + 1",
                    "file": "demo.f90",
                    "line": 10,
                    "col": 3,
                    "endLine": 10,
                    "endCol": 12,
                },
            }
        )
        # MainProgram spans lines 3..13.
        main = Node(
            kind="MainProgram",
            children=[stmt_x, stmt_xeq0, stmt_xinc],
            source=Node.from_json(
                {
                    "kind": "MainProgram",
                    "source": {
                        "text": "program demo",
                        "file": "demo.f90",
                        "line": 3,
                        "col": 1,
                        "endLine": 3,
                        "endCol": 13,
                    },
                }
            ).source,
        )
        return Node(kind="Program", children=[main])

    def test_attaches_trailing_and_leading_comments(self) -> None:
        root = self._make_tree()
        annotate_tree(root, sources={"demo.f90": _FORTRAN_SOURCE})

        # Helpers
        stmts = list(root.find_all("Statement"))
        by_line = {
            s.source.line: s for s in stmts if s.source and s.source.line
        }

        # Line 5 statement has a trailing comment ("the running total")
        s5 = by_line[5]
        self.assertEqual(len(s5.trailing_comments), 1)
        self.assertIn("the running total", s5.trailing_comments[0].text)

        # Line 10 has a trailing comment ("step")
        s10 = by_line[10]
        self.assertEqual(len(s10.trailing_comments), 1)
        self.assertIn("step", s10.trailing_comments[0].text)

        # Line 9 has a leading comment block ("Initialise and increment.")
        s9 = by_line[9]
        self.assertEqual(len(s9.leading_comments), 1)
        self.assertIn("Initialise", s9.leading_comments[0].text)

    def test_leading_block_separated_by_blank_lines_still_attaches(self) -> None:
        # A doc-comment block separated from its statement by one or more
        # blank lines (the ``C ...header...`` / blank / ``IMPLICIT NONE``
        # shape that pervades fixed-form Fortran) must still attach to the
        # following statement, not be orphaned.
        source = (
            "subroutine s\n"          # 1
            "C ====================\n"  # 2
            "C does a thing\n"          # 3
            "C ====================\n"  # 4
            "\n"                        # 5  (blank gap)
            "      integer :: x\n"      # 6
            "      end\n"               # 7
        )
        stmt = Node.from_json({
            "kind": "Statement",
            "source": {"text": "integer :: x", "file": "s.f", "line": 6,
                       "col": 7, "endLine": 6, "endCol": 19},
        })
        prog = Node(
            kind="SubroutineSubprogram", children=[stmt],
            source=Node.from_json({
                "kind": "SubroutineSubprogram",
                "source": {"text": "subroutine s", "file": "s.f", "line": 1,
                           "col": 1, "endLine": 1, "endCol": 13},
            }).source,
        )
        root = Node(kind="Program", children=[prog])
        annotate_tree(root, sources={"s.f": source}, fixed_form=True)
        texts = [c.text.strip() for c in stmt.leading_comments]
        self.assertIn("does a thing", texts)
        self.assertEqual(len(stmt.leading_comments), 3)  # all 3 C-lines

    def test_leading_comments_only_attach_once(self) -> None:
        root = self._make_tree()
        annotate_tree(root, sources={"demo.f90": _FORTRAN_SOURCE})
        # The two-line top-of-file comment should be attached to the outermost
        # construct that begins at line 3 — MainProgram (or Program).
        anchored = [
            n
            for n in root.walk()
            if any(c.line in (1, 2) for c in n.leading_comments)
        ]
        # Each top-of-file comment line gets attached to exactly one node.
        line_assignments: dict[int, list[str]] = {1: [], 2: []}
        for n in anchored:
            for c in n.leading_comments:
                if c.line in line_assignments:
                    line_assignments[c.line].append(n.kind)
        self.assertEqual(len(line_assignments[1]), 1)
        self.assertEqual(len(line_assignments[2]), 1)

    def test_round_trip_json(self) -> None:
        root = self._make_tree()
        annotate_tree(root, sources={"demo.f90": _FORTRAN_SOURCE})
        as_dict = root.to_json()
        as_text = json.dumps(as_dict)
        reloaded = Node.from_json(json.loads(as_text))
        # The reloaded tree should retain its comments.
        original_comments = sorted(
            c.line
            for n in root.walk()
            for c in (*n.leading_comments, *n.trailing_comments)
        )
        reloaded_comments = sorted(
            c.line
            for n in reloaded.walk()
            for c in (*n.leading_comments, *n.trailing_comments)
        )
        self.assertEqual(original_comments, reloaded_comments)

    def test_render_report_lists_attached_comments(self) -> None:
        root = self._make_tree()
        annotate_tree(root, sources={"demo.f90": _FORTRAN_SOURCE})
        report = render_report(root)
        self.assertIn("the running total", report)
        self.assertIn("Initialise", report)

    def test_annotator_loads_source_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "demo.f90"
            path.write_text(_FORTRAN_SOURCE)
            root = self._make_tree()
            # Rewrite source paths to point at the temp file.
            for node in root.walk():
                if node.source and node.source.file == "demo.f90":
                    object.__setattr__(node.source, "file", str(path))
            ann = CommentAnnotator()
            ann.annotate(root)
            self.assertTrue(
                any(node.trailing_comments for node in root.walk()),
                "expected at least one trailing comment",
            )


@unittest.skipUnless(_have_flang(), "flang binary not available")
class FlangCliTests(unittest.TestCase):
    """Drive the full pipeline end-to-end against a real flang build."""

    def test_annotate_via_api(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".f90", delete=False, encoding="utf-8"
        ) as f:
            f.write(_FORTRAN_SOURCE)
            tmp = Path(f.name)
        try:
            root = parse_fortran_file(tmp)
            annotate_tree(root)
            # Look for the inline "step" comment via the AST.
            tail = [
                c.text.strip()
                for n in root.walk()
                for c in n.trailing_comments
            ]
            self.assertIn("step", tail)
            # And the leading comment block on the assignment.
            lead = [
                c.text.strip()
                for n in root.walk()
                for c in n.leading_comments
            ]
            self.assertTrue(any("Initialise" in l for l in lead))
        finally:
            tmp.unlink(missing_ok=True)

    def test_cli_emits_valid_annotated_json(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".f90", delete=False, encoding="utf-8"
        ) as f:
            f.write(_FORTRAN_SOURCE)
            tmp = Path(f.name)
        try:
            env = os.environ.copy()
            env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parent.parent))
            result = subprocess.run(
                [sys.executable, "-m", "flang_ast.annotate", str(tmp)],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            doc = json.loads(result.stdout)
            self.assertEqual(doc["kind"], "Program")
            # Verify at least one comment landed somewhere.
            def has_comments(node: dict) -> bool:
                if node.get("leadingComments") or node.get("trailingComments"):
                    return True
                return any(has_comments(c) for c in node.get("children", []))
            self.assertTrue(has_comments(doc))
        finally:
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

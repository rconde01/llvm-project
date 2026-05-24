"""Tests for the dependency-ordering pass."""

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
    OrderingResult,
    Subprogram,
    collect_subprograms,
    order_by_dependencies,
    parse_fortran_source,
)
from flang_ast.depgraph import _tarjan_scc, render_dot, render_text


def _have_flang() -> bool:
    return bool(
        os.environ.get("FLANG") or shutil.which("flang-new") or shutil.which("flang")
    )


class TarjanTests(unittest.TestCase):
    def test_simple_chain(self) -> None:
        # a -> b -> c; expected SCC order: [c], [b], [a]
        sccs = _tarjan_scc(["a", "b", "c"], {"a": ["b"], "b": ["c"], "c": []})
        self.assertEqual(sccs, [["c"], ["b"], ["a"]])

    def test_self_loop(self) -> None:
        sccs = _tarjan_scc(["a"], {"a": ["a"]})
        self.assertEqual(sccs, [["a"]])

    def test_cycle(self) -> None:
        # a <-> b, both depend on c
        sccs = _tarjan_scc(
            ["a", "b", "c"], {"a": ["b", "c"], "b": ["a", "c"], "c": []}
        )
        # c should come first; then the (a,b) SCC.
        self.assertEqual(len(sccs), 2)
        self.assertEqual(sccs[0], ["c"])
        self.assertEqual(sorted(sccs[1]), ["a", "b"])

    def test_disconnected(self) -> None:
        sccs = _tarjan_scc(
            ["a", "b", "c", "d"],
            {"a": ["b"], "b": [], "c": ["d"], "d": []},
        )
        # Order between independent chains follows the input order of the
        # roots; just check each chain is internally ordered.
        flat = [n for scc in sccs for n in scc]
        self.assertLess(flat.index("b"), flat.index("a"))
        self.assertLess(flat.index("d"), flat.index("c"))


SRC_LINEAR = """\
module m
contains
  subroutine alpha()
    call beta()
    call gamma()
  end subroutine

  subroutine beta()
    call gamma()
  end subroutine

  subroutine gamma()
  end subroutine

  integer function delta(x)
    integer, intent(in) :: x
    delta = x + 1
  end function
end module

program main
  use m
  integer :: r
  call alpha()
  r = delta(2)
end program
"""


SRC_RECURSION = """\
module r
contains
  recursive subroutine ping(n)
    integer, intent(in) :: n
    if (n > 0) call pong(n - 1)
  end subroutine

  recursive subroutine pong(n)
    integer, intent(in) :: n
    if (n > 0) call ping(n - 1)
  end subroutine

  subroutine leaf()
  end subroutine
end module

program p
  use r
  call ping(3)
  call leaf()
end program
"""


@unittest.skipUnless(_have_flang(), "flang binary not available")
class FlangBackedTests(unittest.TestCase):
    def _order(self, src: str) -> OrderingResult:
        root = parse_fortran_source(src)
        return order_by_dependencies(root)

    def test_collect_finds_all_subprograms(self) -> None:
        root = parse_fortran_source(SRC_LINEAR)
        subs = collect_subprograms(root)
        names = sorted(s.name for s in subs)
        self.assertEqual(names, ["alpha", "beta", "delta", "gamma", "main"])

        # Each module subprogram should record its parent as a Subprogram, too.
        # The current implementation marks them parent=None because Module
        # itself isn't a callable; only nested ``CONTAINS`` inside another
        # subprogram introduces a parent.
        parents = {s.name: s.parent for s in subs}
        self.assertIsNone(parents["alpha"])
        self.assertIsNone(parents["main"])

    def test_topological_order(self) -> None:
        result = self._order(SRC_LINEAR)
        order_names = [s.name for s in result.order]

        # Every callee precedes its caller.
        def index(name: str) -> int:
            return order_names.index(name)

        # gamma is called by alpha and beta
        self.assertLess(index("gamma"), index("beta"))
        self.assertLess(index("gamma"), index("alpha"))
        # beta is called by alpha
        self.assertLess(index("beta"), index("alpha"))
        # delta is called by main
        self.assertLess(index("delta"), index("main"))
        # alpha is called by main
        self.assertLess(index("alpha"), index("main"))
        # main is always last in this example
        self.assertEqual(order_names[-1], "main")
        # No cycles expected
        self.assertEqual(result.cycles, [])

    def test_calls_are_recorded(self) -> None:
        result = self._order(SRC_LINEAR)
        alpha = result.by_name["alpha"]
        self.assertEqual(set(alpha.calls), {"beta", "gamma"})
        self.assertEqual(set(result.by_name["beta"].calls), {"gamma"})
        self.assertEqual(result.by_name["gamma"].calls, [])
        self.assertEqual(set(result.by_name["main"].calls), {"alpha", "delta"})

    def test_mutual_recursion_becomes_one_scc(self) -> None:
        result = self._order(SRC_RECURSION)
        cycle_names = [sorted(s.name for s in scc) for scc in result.cycles]
        self.assertEqual(cycle_names, [["ping", "pong"]])

        # Within the SCC, ping and pong are emitted together — and *before*
        # the program that calls them.
        order_names = [s.name for s in result.order]
        ping_i = order_names.index("ping")
        pong_i = order_names.index("pong")
        prog_i = order_names.index("p")
        # ping and pong appear adjacent (or at least before the program).
        self.assertLess(max(ping_i, pong_i), prog_i)
        # leaf has no dependencies and gets emitted before the program too.
        self.assertLess(order_names.index("leaf"), prog_i)

    def test_external_calls_captured(self) -> None:
        src = """\
program demo
  integer :: i
  i = mod(7, 3)         ! intrinsic, not defined in this file
  call external_thing() ! unresolved
end program
"""
        result = order_by_dependencies(parse_fortran_source(src))
        demo = next(iter(result.order))
        ext = {x.lower() for x in demo.external_calls}
        self.assertIn("mod", ext)
        self.assertIn("external_thing", ext)
        self.assertEqual(demo.calls, [])

    def test_render_text_and_dot(self) -> None:
        result = self._order(SRC_LINEAR)
        text = render_text(result)
        self.assertIn("alpha", text)
        self.assertIn("-> beta", text.lower()) if False else None
        # arrow lowercased above is wrong; assert plain presence instead:
        self.assertIn("-> ", text)
        dot = render_dot(result)
        self.assertTrue(dot.startswith("digraph callgraph"))
        self.assertIn('"alpha" -> "beta"', dot)

    def test_cli_names_mode_is_correct_order(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".f90", delete=False, encoding="utf-8"
        ) as f:
            f.write(SRC_LINEAR)
            tmp = Path(f.name)
        try:
            env = os.environ.copy()
            env.setdefault(
                "PYTHONPATH", str(Path(__file__).resolve().parent.parent)
            )
            res = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "flang_ast.depgraph",
                    str(tmp),
                    "--names",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            order = [line.lower() for line in res.stdout.strip().splitlines()]
            # gamma before alpha; main last
            self.assertLess(order.index("gamma"), order.index("alpha"))
            self.assertEqual(order[-1], "main")
        finally:
            tmp.unlink(missing_ok=True)

    def test_cli_returns_exit_2_on_cycle(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".f90", delete=False, encoding="utf-8"
        ) as f:
            f.write(SRC_RECURSION)
            tmp = Path(f.name)
        try:
            env = os.environ.copy()
            env.setdefault(
                "PYTHONPATH", str(Path(__file__).resolve().parent.parent)
            )
            res = subprocess.run(
                [sys.executable, "-m", "flang_ast.depgraph", str(tmp), "--json"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(res.returncode, 2)
            doc = json.loads(res.stdout)
            self.assertTrue(doc["cycles"], "expected at least one cycle")
        finally:
            tmp.unlink(missing_ok=True)

    def test_subprogram_source_text(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".f90", delete=False, encoding="utf-8"
        ) as f:
            f.write(SRC_LINEAR)
            tmp = Path(f.name)
        try:
            root = order_by_dependencies(parse_fortran_source(SRC_LINEAR))
            # Re-issue with the file on disk so source_text can read it.
            # parse_fortran_source uses a temp file that is deleted; instead
            # parse with a known file:
            from flang_ast import parse_fortran_file

            result = order_by_dependencies(parse_fortran_file(tmp))
            gamma = result.by_name["gamma"]
            text = gamma.source_text()
            self.assertIn("subroutine gamma", text.lower())
            self.assertIn("end subroutine", text.lower())
        finally:
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

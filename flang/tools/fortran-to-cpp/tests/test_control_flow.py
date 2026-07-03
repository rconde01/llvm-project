"""Tests for do-while, select-case, cycle/exit, and single-line if."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from converter import convert_file


def _have_flang() -> bool:
    return bool(
        os.environ.get("FLANG") or shutil.which("flang-new") or shutil.which("flang")
    )


def _have_cxx() -> bool:
    return any(shutil.which(n) for n in ("c++", "g++", "clang++"))


RUNTIME_INCLUDE = Path(__file__).resolve().parent.parent / "runtime" / "include"


CONTROL_F90 = """\
program cf
  integer :: i, n
  i = 0
  do while (i < 5)
    i = i + 1
    if (i == 2) cycle
    if (i == 4) exit
    print *, i
  end do
  n = 2
  select case (n)
  case (1)
    print *, "one"
  case (2, 3)
    print *, "two or three"
  case default
    print *, "other"
  end select
end program
"""


RANGE_CASE_F90 = """\
program rc
  integer :: k
  do k = 1, 6
    select case (k)
    case (1:2)
      print *, k, "low"
    case (3:4)
      print *, k, "mid"
    case default
      print *, k, "high"
    end select
  end do
end program
"""


OPEN_RANGE_CASE_F90 = """\
program orc
  integer :: k
  k = 10
  select case (k)
  case (:0)
    print *, "neg"
  case (5:)
    print *, "high"
  case default
    print *, "mid"
  end select
end program
"""


CHAR_CASE_F90 = """\
program cc
  character(len=4) :: c
  c = "yes "
  select case (c)
  case ("yes ")
    print *, "y"
  case ("no  ")
    print *, "n"
  end select
end program
"""


def _convert(src: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".f90", delete=False, encoding="utf-8"
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        return convert_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


# A counted DO whose body reassigns the loop bound.  Fortran fixes the
# iteration count on entry, so this runs exactly (5 - 2 + 1) = 4 times; a
# naive ``for (i = j; i <= k; ++i)`` would re-read the mutated ``k`` and run
# far longer (the SPICE f_spk21 ``DO I=J,K`` that reassigns K each pass).
DO_MODIFIED_BOUND_F90 = """\
      program p
      integer i, j, k, cnt
      j = 2
      k = 5
      cnt = 0
      do i = j, k
         cnt = cnt + 1
         k = 100
      end do
      print *, cnt
      end
"""

# A counted DO whose bound is *not* modified -- must stay the clean form.
DO_NORMAL_F90 = """\
      program p
      integer i, n, s
      n = 5
      s = 0
      do i = 1, n
         s = s + i
      end do
      print *, s
      end
"""


@unittest.skipUnless(_have_flang(), "flang binary not available")
class ControlFlowEmitTests(unittest.TestCase):
    def test_do_while_emits_while(self) -> None:
        cpp = _convert(CONTROL_F90)
        self.assertIn("while (i < 5) {", cpp)

    def test_do_modified_bound_is_frozen(self) -> None:
        # The bound is captured into a temp at loop entry.
        cpp = _convert(DO_MODIFIED_BOUND_F90)
        self.assertIn("const ftn::index_t _do_hi = k;", cpp)
        self.assertIn("i <= _do_hi;", cpp)

    def test_do_normal_bound_not_frozen(self) -> None:
        # An ordinary loop keeps the clean ``i <= n`` form (no temp).
        cpp = _convert(DO_NORMAL_F90)
        self.assertIn("for (i = 1; i <= n; ++i)", cpp)
        self.assertNotIn("_do_hi", cpp)

    def test_cycle_and_exit(self) -> None:
        cpp = _convert(CONTROL_F90)
        self.assertIn("continue;", cpp)
        self.assertIn("break;", cpp)

    def test_select_case_emits_switch(self) -> None:
        # Integer selector with literal-integer cases takes the switch
        # fast path (jump-table-eligible) rather than the if-chain.
        cpp = _convert(CONTROL_F90)
        self.assertIn("switch (n) {", cpp)
        self.assertIn("case 1:", cpp)
        # ``case (2, 3)`` -> two fallthrough labels above one body.
        self.assertIn("case 2:", cpp)
        self.assertIn("case 3:", cpp)
        self.assertIn("default:", cpp)

    def test_select_case_range_expands_to_fallthrough(self) -> None:
        # Bounded literal-integer range -> a real switch with one case
        # label per value in the range, so -O2 can emit a jump table.
        cpp = _convert(RANGE_CASE_F90)
        self.assertIn("switch (k) {", cpp)
        self.assertIn("case 1:", cpp)
        self.assertIn("case 2:", cpp)
        self.assertIn("case 3:", cpp)
        self.assertIn("case 4:", cpp)

    def test_select_case_open_range_uses_ifchain(self) -> None:
        # ``case (:0)`` / ``case (5:)`` can't be enumerated -> if-chain.
        cpp = _convert(OPEN_RANGE_CASE_F90)
        self.assertNotIn("switch (k)", cpp)
        self.assertIn("k <= 0", cpp)
        self.assertIn("k >= 5", cpp)

    def test_select_case_character_uses_ifchain(self) -> None:
        # CHARACTER selector isn't integral -> if-chain.
        cpp = _convert(CHAR_CASE_F90)
        self.assertNotIn("switch (c)", cpp)
        self.assertIn("c == ", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ControlFlowRunTests(unittest.TestCase):
    def _run(self, src: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(src)
            cpp = Path(d) / "out.cpp"
            cpp.write_text(convert_file(f))
            exe = Path(d) / "out"
            cxx = (
                shutil.which("c++")
                or shutil.which("g++")
                or shutil.which("clang++")
            )
            assert cxx is not None
            comp = subprocess.run(
                [cxx, "-std=c++20", "-I", str(RUNTIME_INCLUDE),
                 str(cpp), "-o", str(exe)],
                capture_output=True, text=True, check=False,
            )
            if comp.returncode != 0:
                self.fail(f"compile failed:\n{comp.stderr}\n{cpp.read_text()}")
            run = subprocess.run(
                [str(exe)], capture_output=True, text=True, check=False
            )
            self.assertEqual(run.returncode, 0, msg=run.stderr)
            return run.stdout

    def test_control_flow_runs(self) -> None:
        out = self._run(CONTROL_F90)
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        # do-while prints 1 and 3 (i==2 cycles, i==4 exits), then the
        # select-case prints "two or three".
        self.assertEqual(lines[0], "1")
        self.assertEqual(lines[1], "3")
        self.assertIn("two or three", lines[2])

    def test_range_case_runs(self) -> None:
        out = self._run(RANGE_CASE_F90)
        self.assertIn("low", out)
        self.assertIn("mid", out)
        self.assertIn("high", out)

    def test_do_modified_bound_runs(self) -> None:
        # DO I=2,5 runs 4 times even though the body sets K=100 each pass.
        out = self._run(DO_MODIFIED_BOUND_F90)
        self.assertEqual(out.split(), ["4"])


class SelectCaseSwitchEligibilityTests(unittest.TestCase):
    """Drives ``_emit_select_case`` from hand-built IR so coverage of the
    switch / if-chain fast-path / fallback choice doesn't depend on
    flang being installed."""

    def _emit(self, node):
        from io import StringIO
        from converter.emit import _emit_select_case
        out = StringIO()
        _emit_select_case(out, node, indent=0)
        return out.getvalue()

    def _build(self, *clauses, selector_name="n", default=None):
        from converter.ir import (
            IRAssignment, IRCaseClause, IRLiteral, IRName, IRSelectCase,
        )
        # Body content is irrelevant to the eligibility decision; emit a
        # marker assignment so the chosen path is visible in the output.
        def body(marker):
            return [IRAssignment(IRName("x", "x"), IRLiteral(str(marker)))]
        return IRSelectCase(
            selector=IRName(selector_name, selector_name),
            clauses=[
                IRCaseClause(
                    values=[IRLiteral(str(v)) for v in c.get("values", ())],
                    ranges=[
                        (
                            IRLiteral(str(lo)) if lo is not None else None,
                            IRLiteral(str(hi)) if hi is not None else None,
                        )
                        for lo, hi in c.get("ranges", ())
                    ],
                    body=body(c["body"]),
                )
                for c in clauses
            ],
            default_body=body(default) if default is not None else None,
        )

    def test_single_int_literals_emit_switch(self) -> None:
        node = self._build(
            {"values": [1], "body": 1},
            {"values": [2, 3], "body": 23},
            default=0,
        )
        cpp = self._emit(node)
        self.assertIn("switch (n)", cpp)
        self.assertIn("case 1:", cpp)
        self.assertIn("case 2:", cpp)
        self.assertIn("case 3:", cpp)
        self.assertIn("default:", cpp)

    def test_bounded_range_expands(self) -> None:
        node = self._build(
            {"ranges": [(1, 3)], "body": 1},
            {"ranges": [(5, 7)], "body": 5},
        )
        cpp = self._emit(node)
        self.assertIn("switch (n)", cpp)
        for n in (1, 2, 3, 5, 6, 7):
            self.assertIn(f"case {n}:", cpp)

    def test_open_range_falls_back_to_ifchain(self) -> None:
        node = self._build(
            {"ranges": [(None, 0)], "body": -1},
            {"ranges": [(5, None)], "body": 1},
            default=0,
        )
        cpp = self._emit(node)
        self.assertNotIn("switch", cpp)
        self.assertIn("n <= 0", cpp)
        self.assertIn("n >= 5", cpp)

    def test_wide_range_falls_back_to_ifchain(self) -> None:
        # 1..1000 is well above the 64-label budget.
        node = self._build(
            {"ranges": [(1, 1000)], "body": 1},
            default=0,
        )
        cpp = self._emit(node)
        self.assertNotIn("switch", cpp)
        self.assertIn("n >= 1 && n <= 1000", cpp)

    def test_mixed_values_and_ranges(self) -> None:
        # ``case (1, 3:5, 9)`` -> labels 1, 3, 4, 5, 9.
        node = self._build(
            {"values": [1, 9], "ranges": [(3, 5)], "body": 1},
        )
        cpp = self._emit(node)
        self.assertIn("switch (n)", cpp)
        for n in (1, 3, 4, 5, 9):
            self.assertIn(f"case {n}:", cpp)

    def test_negative_literal_label(self) -> None:
        node = self._build(
            {"values": [-1, -2], "body": 1},
            {"ranges": [(-5, -3)], "body": 5},
        )
        cpp = self._emit(node)
        self.assertIn("switch (n)", cpp)
        for n in (-1, -2, -3, -4, -5):
            self.assertIn(f"case {n}:", cpp)

    def test_non_literal_value_falls_back(self) -> None:
        # A named constant in a case value -> can't read the integer at
        # emit time, so use the if-chain.
        from converter.ir import (
            IRAssignment, IRCaseClause, IRLiteral, IRName, IRSelectCase,
        )
        node = IRSelectCase(
            selector=IRName("n", "n"),
            clauses=[
                IRCaseClause(
                    values=[IRName("kFoo", "kFoo")],
                    body=[IRAssignment(IRName("x", "x"), IRLiteral("1"))],
                ),
            ],
        )
        cpp = self._emit(node)
        self.assertNotIn("switch", cpp)
        self.assertIn("n == kFoo", cpp)


if __name__ == "__main__":
    unittest.main()

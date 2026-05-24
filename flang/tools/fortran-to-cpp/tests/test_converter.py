"""Tests for the fortran-to-cpp converter.

Three layers:
  * Unit tests on the lowering pass (no compiler invocation needed for
    the IR-level checks).
  * End-to-end tests that drive flang + the lowering + the emitter.
  * Compile-and-run tests that verify the emitted C++ builds with the
    runtime and produces the expected output.

The flang-dependent tests are skipped when no flang binary is
discoverable; the compile-and-run tests are additionally skipped if no
C++20 compiler is available.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from converter import convert_file, lower_to_ir
from converter.ir import (
    IRAssignment,
    IRBinaryOp,
    IRDo,
    IRIf,
    IRLiteral,
    IRName,
    IRPrint,
)


def _have_flang() -> bool:
    return bool(
        os.environ.get("FLANG") or shutil.which("flang-new") or shutil.which("flang")
    )


def _have_cxx() -> bool:
    for name in ("c++", "g++", "clang++"):
        if shutil.which(name):
            return True
    return False


RUNTIME_INCLUDE = Path(__file__).resolve().parent.parent / "runtime" / "include"


# ---------------------------------------------------------------------------
# Lowering tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(_have_flang(), "flang binary not available")
class LoweringTests(unittest.TestCase):
    def _lower(self, src: str) -> "IRTranslationUnit":
        with tempfile.NamedTemporaryFile(
            "w", suffix=".f90", delete=False, encoding="utf-8"
        ) as f:
            f.write(src)
            tmp = Path(f.name)
        try:
            return lower_to_ir(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def test_hello_world_lowers_to_print_with_literal(self) -> None:
        tu = self._lower("program p\n  print *, \"hi\"\nend program\n")
        self.assertEqual(len(tu.subprograms), 1)
        sub = tu.subprograms[0]
        self.assertEqual(sub.kind, "main")
        self.assertEqual(len(sub.body), 1)
        prt = sub.body[0]
        self.assertIsInstance(prt, IRPrint)
        assert isinstance(prt, IRPrint)
        self.assertEqual(len(prt.items), 1)
        lit = prt.items[0]
        self.assertIsInstance(lit, IRLiteral)
        assert isinstance(lit, IRLiteral)
        self.assertIn("hi", lit.cpp_text)

    def test_integer_local_gets_int32_type(self) -> None:
        tu = self._lower(
            "program p\n  integer :: n\n  n = 42\nend program\n"
        )
        sub = tu.subprograms[0]
        self.assertEqual(len(sub.locals), 1)
        self.assertEqual(sub.locals[0].name, "n")
        self.assertEqual(sub.locals[0].type.cpp, "std::int32_t")

    def test_real_kind_8_becomes_double(self) -> None:
        tu = self._lower(
            "program p\n  real(kind=8) :: x\n  x = 0\nend program\n"
        )
        sub = tu.subprograms[0]
        self.assertEqual(sub.locals[0].type.cpp, "double")

    def test_character_with_length_becomes_fortran_string(self) -> None:
        tu = self._lower(
            "program p\n"
            "  character(len=10) :: s\n"
            "  s = 'hi'\n"
            "end program\n"
        )
        sub = tu.subprograms[0]
        self.assertEqual(
            sub.locals[0].type.cpp, "fortran::FortranString<10>"
        )

    def test_binary_operators_lower_to_irbinaryop(self) -> None:
        tu = self._lower(
            "program p\n"
            "  integer :: a, b, c\n"
            "  a = 1\n  b = 2\n"
            "  c = a + b * 3\n"
            "end program\n"
        )
        body = tu.subprograms[0].body
        # Last assignment: c = a + b * 3
        last = body[-1]
        self.assertIsInstance(last, IRAssignment)
        assert isinstance(last, IRAssignment)
        self.assertIsInstance(last.value, IRBinaryOp)
        assert isinstance(last.value, IRBinaryOp)
        self.assertEqual(last.value.op, "+")

    def test_do_loop_lowers_to_irdo(self) -> None:
        tu = self._lower(
            "program p\n"
            "  integer :: i, n\n"
            "  n = 5\n"
            "  do i = 1, n\n    print *, i\n  end do\n"
            "end program\n"
        )
        do_loops = [s for s in tu.subprograms[0].body if isinstance(s, IRDo)]
        self.assertEqual(len(do_loops), 1)
        self.assertEqual(do_loops[0].var, "i")

    def test_if_then_else_lowers_to_irif(self) -> None:
        tu = self._lower(
            "program p\n"
            "  integer :: n\n"
            "  n = 0\n"
            "  if (n > 0) then\n"
            "    print *, 'pos'\n"
            "  else\n"
            "    print *, 'nonpos'\n"
            "  end if\n"
            "end program\n"
        )
        ifs = [s for s in tu.subprograms[0].body if isinstance(s, IRIf)]
        self.assertEqual(len(ifs), 1)
        self.assertEqual(len(ifs[0].branches), 1)
        self.assertIsNotNone(ifs[0].else_body)


# ---------------------------------------------------------------------------
# End-to-end source emission
# ---------------------------------------------------------------------------


HELLO_F90 = """\
program hello
  print *, "hello, world"
end program
"""


SUM_F90 = """\
program sum_to_n
  ! Compute 1+2+...+n.
  integer :: i, n, s
  s = 0
  n = 10
  do i = 1, n
    s = s + i   ! accumulate
  end do
  if (s > 50) then
    print *, "big sum:", s
  else
    print *, "small sum:", s
  end if
end program
"""


@unittest.skipUnless(_have_flang(), "flang binary not available")
class EmitTests(unittest.TestCase):
    def _convert(self, src: str) -> str:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".f90", delete=False, encoding="utf-8"
        ) as f:
            f.write(src)
            tmp = Path(f.name)
        try:
            return convert_file(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def test_hello_world_emits_cout_chain(self) -> None:
        cpp = self._convert(HELLO_F90)
        self.assertIn("std::cout <<", cpp)
        self.assertIn('"hello, world"sv', cpp)
        self.assertIn("int main()", cpp)
        self.assertIn("hello()", cpp)

    def test_sum_to_n_translates_full_control_flow(self) -> None:
        cpp = self._convert(SUM_F90)
        self.assertIn("std::int32_t s{};", cpp)
        self.assertIn("for (i = 1; i <= n; ++i)", cpp)
        self.assertIn("s = s + i;", cpp)
        self.assertIn("if (s > 50)", cpp)
        self.assertIn("} else {", cpp)
        # Trailing comment preservation.
        self.assertIn("// accumulate", cpp)


# ---------------------------------------------------------------------------
# Compile-and-run end-to-end
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    _have_flang() and _have_cxx(),
    "need both flang and a C++20 compiler",
)
class CompileAndRunTests(unittest.TestCase):
    def _compile_and_run(self, fortran: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "in.f90"
            src.write_text(fortran)
            cpp = Path(d) / "out.cpp"
            cpp.write_text(convert_file(src))
            exe = Path(d) / "out"
            cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
            assert cxx is not None
            proc = subprocess.run(
                [
                    cxx,
                    "-std=c++20",
                    "-I",
                    str(RUNTIME_INCLUDE),
                    str(cpp),
                    "-o",
                    str(exe),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                self.fail(
                    f"compilation failed:\n{proc.stderr}\nemitted:\n{cpp.read_text()}"
                )
            run = subprocess.run(
                [str(exe)], capture_output=True, text=True, check=False
            )
            self.assertEqual(run.returncode, 0, msg=run.stderr)
            return run.stdout

    def test_hello_world_runs(self) -> None:
        out = self._compile_and_run(HELLO_F90)
        self.assertIn("hello, world", out)

    def test_sum_to_n_runs(self) -> None:
        out = self._compile_and_run(SUM_F90)
        # 1+...+10 = 55, which is > 50, so the "big" branch fires.
        self.assertIn("big sum:", out)
        self.assertIn("55", out)


if __name__ == "__main__":
    unittest.main()

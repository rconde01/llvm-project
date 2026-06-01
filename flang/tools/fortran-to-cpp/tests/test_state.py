"""Tests for the state-plumbing pass (SAVE locals and common blocks)."""

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


SAVE_F90 = """\
subroutine counter()
  integer, save :: n
  n = n + 1
  print *, "count:", n
end subroutine

program demo
  call counter()
  call counter()
  call counter()
end program
"""


SAVE_TRANSITIVE_F90 = """\
subroutine counter()
  integer, save :: n
  n = n + 1
  print *, "n=", n
end subroutine

subroutine indirect()
  call counter()
end subroutine

program demo
  call counter()
  call indirect()
  call counter()
end program
"""


COMMON_F90 = """\
subroutine init()
  common /state/ x, y
  real :: x, y
  x = 1.0
  y = 2.0
end subroutine

subroutine show()
  common /state/ x, y
  real :: x, y
  print *, x, y
end subroutine

program demo
  common /state/ x, y
  real :: x, y
  call init()
  call show()
  x = x + 10
  call show()
end program
"""


# Fortran ``BLOCK DATA`` -- a load-time initializer for COMMON blocks.
# Lowers to a synthetic ``block_data_init_<name>`` routine; main calls
# it before the body runs.  Supports the same-layout case (BLOCK DATA
# and routines declare matching variables at matching positions); the
# multi-layout case (e.g. NRLMSISE's PT1/PT2/PT3 overlaying canonical
# PT) needs byte-offset COMMON modeling and is a known gap.
BLOCK_DATA_F77 = """\
      block data myinit
      common /c/ alpha, beta, n
      real alpha, beta
      integer n
      data alpha/3.14/, beta/2.71/, n/42/
      end
      program p
      common /c/ alpha, beta, n
      real alpha, beta
      integer n
      print *, alpha, beta, n
      end
"""


# The same COMMON block declared with different layouts in two routines,
# where a name (K, IY) appears at *different positions* (IRI's igrf
# ``/C1/`` pattern).  Each position is distinct storage, so the merged
# struct must give the second occurrence a disambiguated field name rather
# than emit a duplicate member (which fails to compile).
COMMON_ALIAS_F90 = """\
subroutine one()
  common /c1/ p, q, k, iy
  real :: p, q
  integer :: k, iy
  k = 1
  iy = 2
end subroutine

subroutine two()
  common /c1/ p, q, r, s, t, k, iy
  real :: p, q, r, s, t
  integer :: k, iy
  k = 9
  iy = 8
  print *, k, iy
end subroutine

program demo
  call one()
  call two()
end program
"""


# A COMMON array declared with a *different shape* in two routines
# (storage association / reshape): ``/w/ a(6)`` vs ``/w/ b(2,3)`` over the
# same 6-float storage (the IRI /BLWRK/ WA(216)-vs-WA(36,6) pattern).  The
# second routine must view the shared storage with its own rank/extents
# (column-major), not be bound by reference to the 1-D canonical field.
COMMON_RESHAPE_F90 = """\
subroutine setit()
  common /w/ a(6)
  real :: a
  integer :: i
  do i = 1, 6
    a(i) = i + 0.5
  end do
end subroutine

subroutine showit()
  common /w/ b(2, 3)
  real :: b
  print *, b(1, 1), b(2, 1), b(1, 2), b(2, 3)
end subroutine

program demo
  call setit()
  call showit()
end program
"""


@unittest.skipUnless(_have_flang(), "flang binary not available")
class StateEmitTests(unittest.TestCase):
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

    def test_save_generates_struct_and_param(self) -> None:
        cpp = self._convert(SAVE_F90)
        self.assertIn("struct CounterSave {", cpp)
        self.assertIn("std::int32_t n{};", cpp)
        self.assertIn("void counter(CounterSave& counter_save)", cpp)
        # State bound with auto& so the body stays clean.
        self.assertIn("auto& n = counter_save.n;", cpp)
        self.assertIn("n = n + 1;", cpp)
        # Main owns the instance and threads it through.
        self.assertIn("CounterSave counter_save", cpp)
        self.assertIn("counter(counter_save);", cpp)

    def test_save_transitive_forwarding(self) -> None:
        cpp = self._convert(SAVE_TRANSITIVE_F90)
        # indirect() doesn't own the save struct but must forward it.
        self.assertIn("void indirect(CounterSave& counter_save)", cpp)
        self.assertIn("counter(counter_save);", cpp)

    def test_common_block_generates_shared_struct(self) -> None:
        cpp = self._convert(COMMON_F90)
        self.assertIn("struct StateCommon {", cpp)
        self.assertIn("float x{};", cpp)
        self.assertIn("float y{};", cpp)
        # Non-main routines take the struct as a parameter.
        self.assertIn("void init(StateCommon& state_common)", cpp)
        self.assertIn("void show(StateCommon& state_common)", cpp)
        # Main owns the instance (no parameter on the main program).
        self.assertNotIn("void demo(StateCommon", cpp)
        self.assertIn("StateCommon state_common", cpp)
        # Members bound with auto&; body uses the bare names.
        self.assertIn("auto& x = state_common.x;", cpp)
        self.assertIn("x = 1.0f;", cpp)

    def test_common_reshaped_member_uses_arrayref_view(self) -> None:
        # showit declares /w/ as b(2,3); it must bind a rank-2 ArrayRef view
        # over the shared storage, not an auto& to the 1-D canonical field.
        cpp = self._convert(COMMON_RESHAPE_F90)
        self.assertIn(
            "auto b = fortran::ArrayRef<float, 2>(w_common.a.data(), {2, 3});",
            cpp,
        )

    def test_block_data_emits_init_routine(self) -> None:
        # BLOCK DATA myinit -> ``block_data_init_myinit(c_common)``; main
        # calls it before its own body so the COMMON struct is initialized.
        cpp = self._convert(BLOCK_DATA_F77)
        self.assertIn("void block_data_init_myinit(CCommon& c_common)", cpp)
        self.assertIn("block_data_init_myinit(c_common);", cpp)

    def test_common_repeated_name_at_two_offsets_disambiguated(self) -> None:
        # K/IY at different byte offsets in the two layouts.  Byte-offset
        # COMMON modeling picks the longer layout (``two``'s p,q,r,s,t,k,iy)
        # as canonical and binds ``one``'s shorter list (p,q,k,iy) to the
        # canonical fields that occupy those byte offsets: ``one``'s k /
        # iy land on canonical ``r`` / ``s`` (offsets 8/12), reinterpreted
        # to ``one``'s INTEGER element type (Fortran storage association
        # preserves the routine's view of the bytes).  Routine ``two``'s
        # k / iy bind to canonical ``k`` / ``iy`` at offsets 20/24.
        cpp = self._convert(COMMON_ALIAS_F90)
        self.assertIn("struct C1Common {", cpp)
        # No disambiguated field needed -- each offset has one canonical name.
        self.assertEqual(cpp.count("std::int32_t k{};"), 1)
        # one()'s "k" and "iy" land on canonical "r" and "s" via a type-pun
        # reinterpret so the routine's INTEGER writes land as INTEGER bytes.
        self.assertIn(
            "auto& k = *reinterpret_cast<std::int32_t*>(&c1_common.r);", cpp
        )
        self.assertIn(
            "auto& iy = *reinterpret_cast<std::int32_t*>(&c1_common.s);", cpp
        )


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class StateRunTests(unittest.TestCase):
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

    def test_save_state_persists(self) -> None:
        out = self._run(SAVE_F90)
        self.assertIn("count: 1", out)
        self.assertIn("count: 2", out)
        self.assertIn("count: 3", out)

    def test_save_transitive_runs(self) -> None:
        out = self._run(SAVE_TRANSITIVE_F90)
        lines = [l for l in out.splitlines() if l.strip()]
        # counter called 3 times total -> n = 1, 2, 3 in order.
        self.assertIn("n= 1", lines[0])
        self.assertIn("n= 2", lines[1])
        self.assertIn("n= 3", lines[2])

    def test_common_reshaped_member_runs(self) -> None:
        # a(6) filled 1.5..6.5; the 2-D view (column-major) reads:
        # b(1,1)=a(1)=1.5, b(2,1)=a(2)=2.5, b(1,2)=a(3)=3.5, b(2,3)=a(6)=6.5.
        self.assertEqual(
            self._run(COMMON_RESHAPE_F90).split(),
            ["1.5", "2.5", "3.5", "6.5"],
        )

    def test_block_data_runs(self) -> None:
        # The BLOCK DATA-initialized COMMON values reach the main body
        # before any executable statement (loaded by the synthetic init).
        out = self._run(BLOCK_DATA_F77).split()
        self.assertAlmostEqual(float(out[0]), 3.14, places=2)
        self.assertAlmostEqual(float(out[1]), 2.71, places=2)
        self.assertEqual(out[2], "42")

    def test_common_repeated_name_runs(self) -> None:
        # With the duplicate member disambiguated, the program builds; K/IY
        # at the second layout's offsets read back the values ``two`` set.
        out = self._run(COMMON_ALIAS_F90).split()
        self.assertEqual(out, ["9", "8"])

    def test_common_block_shares_state(self) -> None:
        out = self._run(COMMON_F90)
        lines = [l for l in out.splitlines() if l.strip()]
        # First show: x=1, y=2.  After x=x+10: x=11, y=2.
        self.assertIn("1", lines[0])
        self.assertIn("2", lines[0])
        self.assertIn("11", lines[1])

    def test_two_independent_program_states(self) -> None:
        """The whole point of D2.b: two simultaneous states don't
        interfere.  We drive the common-block program's routines with
        two separate struct instances."""
        # This is exercised implicitly by the struct-based design; here
        # we just confirm the emitted struct is a plain value type with
        # no global/static storage.
        cpp = convert_file_str(COMMON_F90)
        self.assertNotIn("static ", cpp)
        self.assertNotIn("thread_local", cpp)


def convert_file_str(src: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".f90", delete=False, encoding="utf-8"
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        return convert_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

"""Tests for EQUIVALENCE.

Two cases are distinguished at lowering time:

* When every aliased member has the same element type *and* the same
  byte size, the equivalence is just a memory-sharing rename -- emit
  ONE local plus an ``auto& alias = primary;`` binding for each other
  name (zero overhead, no proxies).
* When the members differ (the SPICE DAF type-pun pattern -- a
  ``DOUBLE PRECISION`` array aliased to an ``INTEGER`` one), emit a
  per-routine struct with a shared ``std::byte`` buffer and one
  ``EquivArray`` / ``EquivSlot`` proxy per member; the proxies route
  reads / writes through ``memcpy`` so the type pun is well-defined
  under strict aliasing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from converter import ConversionError, convert_file


def _have_flang() -> bool:
    return bool(
        os.environ.get("FLANG") or shutil.which("flang-new") or shutil.which("flang")
    )


def _have_cxx() -> bool:
    return any(shutil.which(n) for n in ("c++", "g++", "clang++"))


RUNTIME_INCLUDE = Path(__file__).resolve().parent.parent / "runtime" / "include"


# Same element type and same size: pure memory-sharing rename.  Lowering
# should keep ONE local and emit a reference binding for the alias.
SAME_TYPE_F = """\
      program p
      real a(4), b(4)
      equivalence (a, b)
      a(1) = 7.5
      b(2) = 9.5
      print *, a(1), a(2), b(1), b(2)
      end
"""


# Different element types / different byte size: real type pun.  Lowering
# should emit an equiv struct with a shared byte buffer and one
# ``EquivArray`` per member.  Reading ``IBUF(8)`` after writing
# ``DBUF(4) = 4.5`` recovers the high 32 bits of IEEE-754 4.5
# (0x40120000 = 1074921472).
PUN_ARRAY_F = """\
      program p
      double precision dbuf(4)
      integer*4        ibuf(8)
      equivalence (dbuf, ibuf)
      dbuf(1) = 1.5
      dbuf(2) = 2.5
      dbuf(3) = 3.5
      dbuf(4) = 4.5
      print *, ibuf(1), ibuf(8)
      end
"""


# Element-to-element copy *within* a type-punned EquivArray view:
# ``IBUF(1) = IBUF(3)``.  Both sides are ``EquivArray::Cell`` proxies; the
# assignment must copy the cell's *value*, not the proxy's byte pointer.
# (The implicit copy-assignment rebinds the pointer -- a silent no-op, the
# same proxy-shadowing bug fixed in ArrayRef / CharRef / Substring.)
PUN_CELL_COPY_F = """\
      program p
      integer i(4)
      real    r(4)
      equivalence (i, r)
      i(1) = 100
      i(2) = 200
      i(3) = 300
      i(4) = 400
      i(1) = i(3)
      print *, i(1), i(2), i(3), i(4)
      end
"""


# Passing an equivalenced (type-punned) array to a subroutine with an
# array dummy -- the SPICE DAF pattern (ZZDAFGSR passes DPBUF to ZZXLATED).
# The EquivArray must view as an ArrayRef so the callee writes T directly
# into the shared buffer; the other (integer) view then reads the bytes.
PUN_PASS_F = """\
      program p
      double precision dbuf(2)
      integer*4        ibuf(4)
      equivalence (dbuf, ibuf)
      call fill(dbuf)
      print *, ibuf(1), ibuf(2)
      end

      subroutine fill(x)
      double precision x(2)
      x(1) = 1.5
      x(2) = 2.5
      return
      end
"""


# Sequence association on an equivalenced element: ``CALL FILL2(DBUF(2))``
# with an array dummy -- exercises ``elem_tail`` over the EquivArray view
# (the DAF ``ZZDAFGSR`` pattern of handing a buffer element to a helper).
PUN_ELEM_TAIL_F = """\
      program p
      double precision dbuf(3)
      integer*4        ibuf(6)
      equivalence (dbuf, ibuf)
      dbuf(1) = 9.5
      call fill2 ( dbuf(2) )
      print *, ibuf(3), ibuf(4)
      end

      subroutine fill2 ( x )
      double precision x(2)
      x(1) = 1.5
      x(2) = 2.5
      return
      end
"""


# Element-subscript alias: ``EQUIVALENCE (BEGIN, PTR(1))`` (SPICE's
# lbins_1.for pattern).  Same scalar type on both sides -- ``BEGIN``
# becomes ``auto& BEGIN = PTR(1);`` (a reference to the existing array
# element); the array PTR keeps its own storage.
ELEMENT_ALIAS_F = """\
      program p
      integer begin, end, ptr(2)
      equivalence ( begin, ptr(1) )
      equivalence ( end,   ptr(2) )
      ptr(1) = 10
      ptr(2) = 20
      print *, begin, end
      end
"""


# Multi-rank EQUIVALENCE is genuinely not modeled (would need a 2-D
# byte-view), so lowering must raise.
MULTI_RANK_F = """\
      program p
      real a(2,3), b(2,3)
      equivalence (a, b)
      print *, a(1,1)
      end
"""


def _convert(src: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".f", delete=False, encoding="utf-8"
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        return convert_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


@unittest.skipUnless(_have_flang(), "flang binary not available")
class EquivalenceEmitTests(unittest.TestCase):
    def test_same_type_emits_reference_binding(self) -> None:
        cpp = _convert(SAME_TYPE_F)
        # One canonical local, then a ref-binding alias -- no proxy struct.
        self.assertIn("ftn::Array<float, 1> a{{4}};", cpp)
        self.assertIn("auto& b = a;", cpp)
        self.assertNotIn("EquivArray", cpp)
        self.assertNotIn("EquivSlot", cpp)

    def test_pun_emits_shared_byte_buffer(self) -> None:
        cpp = _convert(PUN_ARRAY_F)
        # Shared 32-byte buffer (4 * 8 == 8 * 4) -- one EquivArray per name.
        self.assertIn("alignas(8) std::byte _store[32]", cpp)
        self.assertIn("ftn::EquivArray<double, 4, 0> dbuf{_store};", cpp)
        self.assertIn(
            "ftn::EquivArray<int32_t, 8, 0> ibuf{_store};", cpp
        )
        # And the binding so the body keeps using bare names.
        self.assertIn("auto& dbuf = _eq0.dbuf;", cpp)
        self.assertIn("auto& ibuf = _eq0.ibuf;", cpp)
        # Critically: the original locals must NOT be re-declared after
        # the alias (which would shadow the EquivArray and silently
        # ignore the equivalence).
        self.assertNotIn("ftn::Array<double, 1> dbuf{{4}};", cpp)
        self.assertNotIn("ftn::Array<int32_t, 1> ibuf{{8}};", cpp)

    def test_element_alias_binds_to_array_slot(self) -> None:
        # The array's storage IS the storage; the bare-name aliases bind
        # to the existing array slots and the array itself stays declared.
        cpp = _convert(ELEMENT_ALIAS_F)
        self.assertIn("auto& begin = ptr(1);", cpp)
        self.assertIn("auto& end = ptr(2);", cpp)
        # The bare-name locals must not be re-declared by implicit
        # typing -- they're now references into the array.
        self.assertNotIn("int32_t begin{};", cpp)
        self.assertNotIn("int32_t end{};", cpp)
        # The array itself must keep its own declaration.
        self.assertIn("ftn::Array<int32_t, 1> ptr{{2}};", cpp)

    def test_multi_rank_errors(self) -> None:
        with self.assertRaises(ConversionError):
            _convert(MULTI_RANK_F)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class EquivalenceRunTests(unittest.TestCase):
    def _build_and_run(self, src: str) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert(src))
            exe = Path(d) / "out"
            cxx = (
                shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
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
                [str(exe)], capture_output=True, text=True, check=False, cwd=d
            )
            self.assertEqual(run.returncode, 0, msg=run.stderr)
            return run.stdout.split()

    def test_same_type_alias_runs(self) -> None:
        # Cross-name reads must see each other's writes.
        self.assertEqual(
            self._build_and_run(SAME_TYPE_F),
            ["7.5", "9.5", "7.5", "9.5"],
        )

    def test_pun_cell_to_cell_copy_runs(self) -> None:
        # i(1) = i(3): both EquivArray::Cell proxies; i(1) must become 300,
        # not stay 100 (the implicit copy-assign would no-op the value copy).
        self.assertEqual(
            self._build_and_run(PUN_CELL_COPY_F),
            ["300", "200", "300", "400"],
        )

    def test_pun_array_runs(self) -> None:
        # IEEE 4.5 = 0x4012000000000000; high 32 bits (little-endian) =
        # 0x40120000 = 1074921472 in IBUF(8); IBUF(1) lands on the low
        # half of DBUF(1)=1.5 which is exactly 0.
        self.assertEqual(
            self._build_and_run(PUN_ARRAY_F),
            ["0", "1074921472"],
        )

    def test_element_alias_runs(self) -> None:
        # Writes through PTR(1) / PTR(2), reads through the bare-name
        # aliases BEGIN / END -- must observe each other since they
        # share storage.
        self.assertEqual(
            self._build_and_run(ELEMENT_ALIAS_F),
            ["10", "20"],
        )

    def test_pun_element_tail_to_subroutine_runs(self) -> None:
        # FILL2 receives DBUF(2) as an array dummy -> elem_tail views the
        # equivalenced buffer from element 2; writes land in DBUF(2..3) and
        # the integer view reads the punned bytes of DBUF(2)=1.5: low 32 ->
        # IBUF(3)=0, high 32 -> IBUF(4)=0x3FF80000=1073217536.
        self.assertEqual(
            self._build_and_run(PUN_ELEM_TAIL_F),
            ["0", "1073217536"],
        )

    def test_pun_passed_to_subroutine_runs(self) -> None:
        # FILL writes DBUF (passed as an array dummy -> EquivArray viewed
        # as ArrayRef); the integer view reads the punned bytes.  IEEE 1.5
        # = 0x3FF8000000000000: low 32 of DBUF(1) -> IBUF(1) = 0, high 32
        # -> IBUF(2) = 0x3FF80000 = 1073217536.
        self.assertEqual(
            self._build_and_run(PUN_PASS_F),
            ["0", "1073217536"],
        )


if __name__ == "__main__":
    unittest.main()

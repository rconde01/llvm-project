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


# A partial-overlap subscript like ``EQUIVALENCE (A(2), B)`` is not
# modeled -- lowering must raise rather than silently get it wrong.
PARTIAL_OVERLAP_F = """\
      program p
      real a(4), b
      equivalence (a(2), b)
      a(2) = 3.5
      print *, b
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
        self.assertIn("fortran::Array<float, 1> a{{4}};", cpp)
        self.assertIn("auto& b = a;", cpp)
        self.assertNotIn("EquivArray", cpp)
        self.assertNotIn("EquivSlot", cpp)

    def test_pun_emits_shared_byte_buffer(self) -> None:
        cpp = _convert(PUN_ARRAY_F)
        # Shared 32-byte buffer (4 * 8 == 8 * 4) -- one EquivArray per name.
        self.assertIn("alignas(8) std::byte _store[32]", cpp)
        self.assertIn("fortran::EquivArray<double, 4, 0> dbuf{_store};", cpp)
        self.assertIn(
            "fortran::EquivArray<std::int32_t, 8, 0> ibuf{_store};", cpp
        )
        # And the binding so the body keeps using bare names.
        self.assertIn("auto& dbuf = _eq0.dbuf;", cpp)
        self.assertIn("auto& ibuf = _eq0.ibuf;", cpp)
        # Critically: the original locals must NOT be re-declared after
        # the alias (which would shadow the EquivArray and silently
        # ignore the equivalence).
        self.assertNotIn("fortran::Array<double, 1> dbuf{{4}};", cpp)
        self.assertNotIn("fortran::Array<std::int32_t, 1> ibuf{{8}};", cpp)

    def test_partial_overlap_errors(self) -> None:
        with self.assertRaises(ConversionError):
            _convert(PARTIAL_OVERLAP_F)


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

    def test_pun_array_runs(self) -> None:
        # IEEE 4.5 = 0x4012000000000000; high 32 bits (little-endian) =
        # 0x40120000 = 1074921472 in IBUF(8); IBUF(1) lands on the low
        # half of DBUF(1)=1.5 which is exactly 0.
        self.assertEqual(
            self._build_and_run(PUN_ARRAY_F),
            ["0", "1074921472"],
        )


if __name__ == "__main__":
    unittest.main()

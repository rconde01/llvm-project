"""Tests for OPEN/CLOSE and unit-directed file I/O.

File units are modeled as a ``fortran::io::Units`` table threaded through
the call graph as caller-owned state (decision D2.b) -- no globals, so
the I/O stays thread-safe and supports multiple simultaneous program
states.  Routines doing unit I/O receive a ``Units&`` parameter; the top
of the call chain owns the table.
"""

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


ROUNDTRIP_F = """\
      program rt
      call wr
      call rd
      end
      subroutine wr
      open(10, file='fc_test.dat', status='replace')
      write(10, *) 42, 3.5
      close(10)
      end
      subroutine rd
      open(11, file='fc_test.dat', status='old')
      read(11, *) k, x
      close(11)
      write(6, *) k, x
      end
"""


# A read loop that uses ``END=label`` to break on EOF, the Numerical
# Recipes / IRI readapf107 pattern.  Without the END= clause being
# honored, the C++ loops forever reading past the file end (writing
# garbage values, overflowing the destination array, then segfaulting
# or hitting the runtime bounds check).
# Sequential ``READ(unit, fmt)`` with a fixed-width FORMAT.  The naive
# ``stream >> v`` translation space-tokenizes, which would misparse a
# column-packed record like ``-11257.0262.5241.9`` (no whitespace between
# fields).  The converter slices by offset using read_field_int /
# read_field_real instead, matching Fortran's column-positional semantics.
READ_FORMATTED_FIXED_WIDTH_F = """\
      program p
      integer ny, nm, nd, ix
      real    fa, fb, fc

      open(13, file='packed.dat', status='replace')
c     One column-packed record with no whitespace between fields --
c     I3 ints and F5.1 floats run flush against each other (-99 + 257.0
c     come out as "-99257.0").  A naive ``stream >> v`` would misparse it.
      write(13, '(3I3,I3,3F5.1)') 58, 1, 1, -99, 257.0, 262.5, 241.9
      close(13)

      open(13, file='packed.dat', status='old')
      read(13, 10) ny, nm, nd, ix, fa, fb, fc
 10   format(3I3,I3,3F5.1)
      close(13)
      write(*, *) ny, nm, nd, ix, fa, fb, fc
      end
"""


# A FORMAT whose data-descriptor count is less than the item list cycles
# to a new record per cycle (MSIS-86 reads its 1464-element coefficient
# table with ``FORMAT(1X,5E13.6)`` over 293 lines).  The converter must
# emit one ``std::getline`` per format cycle instead of giving up and
# falling back to ``>>``.
READ_FORMAT_CYCLING_F = """\
      program p
      real a(7), b(2,3)
      open(13, file='cycle.dat', status='replace')
      write(13, '(3F8.2)') 1.0, 2.0, 3.0
      write(13, '(3F8.2)') 4.0, 5.0, 6.0
      write(13, '(3F8.2)') 7.0, 11.0, 21.0
      write(13, '(3F8.2)') 12.0, 22.0, 13.0
      write(13, '(3F8.2)') 23.0, 0.0, 0.0
      close(13)

c     The FORMAT below has 3 data descriptors but the item list expands
c     to 7 + 6 = 13 items.  Cycling reads 5 records: 3+3+3+3+1.
      open(13, file='cycle.dat', status='old')
      read(13, '(3F8.2)') a, b
      close(13)
      write(*, *) a(1), a(7), b(1,1), b(2,3)
      end
"""


# Fortran's ``/`` terminator in list-directed input ends the list early
# and leaves remaining items at their **current** values.  C++11's
# ``operator>>`` zeros the target on failure (since C++11), so the
# converter routes through ``fortran::io::read_list_item`` which saves
# the destination and restores it if ``>>`` fails.
READ_SLASH_TERMINATOR_F = """\
      program p
      integer a, b, c
      a = 10
      b = 20
      c = 30
      read(*, *) a, b, c
      write(*, *) a, b, c
      end
"""


READ_END_LABEL_F = """\
      program p
      integer x, n, buf(10)
      open(13, file='end_test.dat', status='replace')
      do n=1,5
        write(13,*) n*10
      end do
      close(13)

      open(13, file='end_test.dat', status='old')
      n = 0
  1   read(13, *, end=21) x
      n = n + 1
      buf(n) = x
      goto 1
 21   close(13)
      write(*,*) n, buf(1), buf(n)
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
class FileIoEmitTests(unittest.TestCase):
    def test_open_close_map_to_units(self) -> None:
        cpp = _convert(ROUNDTRIP_F)
        self.assertIn('_units.open(10, "fc_test.dat"sv, "replace"sv);', cpp)
        self.assertIn("_units.close(10);", cpp)

    def test_unit_io_uses_units_table(self) -> None:
        cpp = _convert(ROUNDTRIP_F)
        self.assertIn("_units.out(10)", cpp)
        self.assertIn("_units.in(11)", cpp)
        # Unit 6 stays a plain stdout write (no units threading needed).
        self.assertIn("std::cout", cpp)

    def test_units_threaded_as_state(self) -> None:
        cpp = _convert(ROUNDTRIP_F)
        # Routines doing unit I/O take a Units& param; main owns the table.
        self.assertIn("void wr(fortran::io::Units& _units)", cpp)
        self.assertIn("fortran::io::Units _units", cpp)  # owned by main
        self.assertIn("wr(_units);", cpp)

    def test_read_end_label_emits_eof_jump(self) -> None:
        # READ(..., END=21) must produce a synthetic ``if (!stream) goto
        # 21;`` so the EOF terminates the read loop rather than spinning
        # forever past the file end.  The structuring pass converts the
        # goto to a state-machine arm, so we just assert that the stream
        # is checked.
        cpp = _convert(READ_END_LABEL_F)
        self.assertIn("(!_units.in(13))", cpp)

    def test_formatted_fixed_width_read_uses_field_slicer(self) -> None:
        # A sequential READ with a constant fixed-width FORMAT must route
        # through ``getline`` + ``read_field_int`` / ``read_field_real``
        # so column-packed records (no whitespace between fields) parse
        # correctly -- not the ``>>`` chain, which would space-tokenize.
        cpp = _convert(READ_FORMATTED_FIXED_WIDTH_F)
        self.assertIn("std::getline(_units.in(13), _rec)", cpp)
        self.assertIn("fortran::io::read_field_int(_rec,", cpp)
        self.assertIn("fortran::io::read_field_real(_rec,", cpp)
        # And no ``>>`` chain for the fixed-width read.
        self.assertNotIn("_units.in(13) >> ny", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class FileIoRunTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert(ROUNDTRIP_F))
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
            # Run in the temp dir so the relative data file lands there.
            run = subprocess.run(
                [str(exe)], capture_output=True, text=True, check=False, cwd=d
            )
            self.assertEqual(run.returncode, 0, msg=run.stderr)
            parts = run.stdout.split()
            self.assertEqual(parts[0], "42")
            self.assertEqual(parts[1], "3.5")

    def test_slash_terminator_preserves_current_values(self) -> None:
        # ``read(*, *) a, b, c`` with input ``5 /`` reads a=5, leaves b
        # and c at their previous values (10 and 30 from initialization).
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert(READ_SLASH_TERMINATOR_F))
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
                [str(exe)], capture_output=True, text=True, check=False, cwd=d,
                input="5 /\n",
            )
            self.assertEqual(run.returncode, 0, msg=run.stderr)
            parts = run.stdout.split()
            # a was read (5); b and c keep their initial values (20, 30).
            self.assertEqual(parts, ["5", "20", "30"])

    def test_format_cycling_read_runs(self) -> None:
        # A FORMAT whose data-descriptor count is less than the item list
        # cycles to a new record per cycle.  Without cycling support the
        # converter falls back to ``>>`` and reads garbage.
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert(READ_FORMAT_CYCLING_F))
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
            parts = run.stdout.split()
            # a(1)=1.0, a(7)=7.0, b(1,1)=11.0 (8th item), b(2,3)=23.0 (13th)
            self.assertAlmostEqual(float(parts[0]), 1.0)
            self.assertAlmostEqual(float(parts[1]), 7.0)
            self.assertAlmostEqual(float(parts[2]), 11.0)
            self.assertAlmostEqual(float(parts[3]), 23.0)

    def test_formatted_fixed_width_read_runs(self) -> None:
        # Round-trip: write a column-packed record via FORMAT, then read
        # it back via FORMAT.  The C++ must reproduce the integer (-112)
        # and the three floats (257.0, 262.5, 241.9) exactly.
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert(READ_FORMATTED_FIXED_WIDTH_F))
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
            parts = run.stdout.split()
            self.assertEqual(parts[:4], ["58", "1", "1", "-99"])
            # Floats round-trip numerically; list-directed print may
            # drop a trailing zero, so compare via float().
            self.assertEqual(float(parts[4]), 257.0)
            self.assertEqual(float(parts[5]), 262.5)
            self.assertAlmostEqual(float(parts[6]), 241.9, places=1)

    def test_read_end_label_runs(self) -> None:
        # Read until EOF and report n + first/last value -- 5, 10, 50.
        # Without END= honored this loops forever / segfaults.
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert(READ_END_LABEL_F))
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
            parts = run.stdout.split()
            self.assertEqual(parts, ["5", "10", "50"])


if __name__ == "__main__":
    unittest.main()

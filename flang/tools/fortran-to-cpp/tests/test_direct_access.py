"""Tests for ACCESS='DIRECT' formatted record reads.

A direct-access OPEN gives each record a fixed RECL-byte slot; a
``READ(unit, fmt, REC=n)`` fetches record ``n`` and parses fixed-width
fields out of it (see ``fortran::io::Units::read_record`` and the
``read_field_*`` helpers in runtime/io.hpp).  This is how the IRI
atmospheric models read their ``ap.dat`` geomagnetic-index file.
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


# Reads record 2 of a 12-char-record direct file (RECL=13 counts the
# trailing newline) with format ``(2I3, F6.1)``.
DIRECT_F = """\
      program da
      real x
      open(9, file='da_test.dat', access='direct', recl=13,
     *     form='formatted', status='old')
      read(9, 10, rec=2) i, j, x
10    format(2i3, f6.1)
      close(9)
      write(6, *) i, j, x
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
class DirectAccessEmitTests(unittest.TestCase):
    def test_open_passes_access_and_recl(self) -> None:
        cpp = _convert(DIRECT_F)
        self.assertIn(
            '_units.open(9, "da_test.dat"sv, "old"sv, "direct"sv, 13);', cpp
        )

    def test_read_uses_read_record_and_field_parsers(self) -> None:
        cpp = _convert(DIRECT_F)
        self.assertIn("_units.read_record(9, 2)", cpp)
        # Two I3 ints at offsets 0 and 3, an F6.1 real at offset 6.
        self.assertIn("fortran::io::read_field_int(_rec, 0, 3)", cpp)
        self.assertIn("fortran::io::read_field_int(_rec, 3, 3)", cpp)
        self.assertIn("fortran::io::read_field_real(_rec, 6, 6, 1)", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class DirectAccessRunTests(unittest.TestCase):
    def test_reads_correct_record(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            # Each record is 12 content chars + newline = 13 bytes (RECL).
            (Path(d) / "da_test.dat").write_text(
                "  1  2  10.5\n"
                "  3  4  20.5\n"
                "  5  6  30.5\n"
            )
            cpp = Path(d) / "out.cpp"
            cpp.write_text(_convert(DIRECT_F))
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
            # Record 2: i=3, j=4, x=20.5
            self.assertEqual(run.stdout.split(), ["3", "4", "20.5"])


if __name__ == "__main__":
    unittest.main()

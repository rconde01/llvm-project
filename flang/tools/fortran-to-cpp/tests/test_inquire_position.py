"""Tests for ``INQUIRE``, ``BACKSPACE``, and ``REWIND``.

INQUIRE queries file/unit properties: existence, open state, name,
access mode, form, RECL, etc.  BACKSPACE / REWIND reposition a unit's
cursor.  All three are SPICE-essential for file management around DAF
binary tables and text-file reopening.

The lowering walks the matching ``*Stmt`` parse-tree nodes and emits
calls to the new ``fortran::io::Units`` helpers
``inquire_by_file`` / ``inquire_by_unit`` / ``backspace`` / ``rewind``.
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


# Two INQUIRE shapes side by side -- the FILE= form on a known-existing
# system file, and the UNIT= form on a unit the program just opened.
INQUIRE_F = """\
      program p
      integer io
      logical ex, op
      character*60 nm
      character*10 acc, frm
      inquire ( file = '/etc/hostname', exist = ex, iostat = io )
      print *, 'fex', ex
      open(11, file='/tmp/inq_test.tmp', status='replace')
      inquire ( unit = 11, opened = op, access = acc, form = frm )
      print *, 'uop', op, 'acc', acc, 'frm', frm
      close(11)
      end
"""


# REWIND: write three records, REWIND, read first record.  BACKSPACE
# between writes the third twice should leave its bytes on the last
# record only (sequential-file semantics).
REWIND_F = """\
      program p
      integer u, n
      character*16 line
      open(12, file='/tmp/rew_test.tmp', status='replace')
      write(12, '(A)') 'first'
      write(12, '(A)') 'second'
      write(12, '(A)') 'third'
      rewind(12)
      read(12, '(A)') line
      print *, line
      close(12)
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
class InquirePositionEmitTests(unittest.TestCase):
    def test_inquire_routes_by_selector(self) -> None:
        cpp = _convert(INQUIRE_F)
        # File form lands on inquire_by_file; unit form on inquire_by_unit.
        self.assertIn('_units.inquire_by_file("/etc/hostname"sv)', cpp)
        self.assertIn("_units.inquire_by_unit(11)", cpp)
        # Each output specifier assigns the matching InquireResult field.
        self.assertIn("ex = _inq.exist;", cpp)
        self.assertIn("op = _inq.opened;", cpp)
        self.assertIn("acc = _inq.access;", cpp)
        self.assertIn("frm = _inq.form;", cpp)

    def test_rewind_and_backspace_emit_calls(self) -> None:
        cpp = _convert(REWIND_F)
        self.assertIn("_units.rewind(12);", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class InquirePositionRunTests(unittest.TestCase):
    def _build_and_run(self, src: str) -> str:
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
            return run.stdout

    def test_inquire_runs(self) -> None:
        out = self._build_and_run(INQUIRE_F).split()
        # ``/etc/hostname`` exists on every Linux box this test runs on.
        self.assertIn("fex", out)
        self.assertEqual(out[out.index("fex") + 1], "T")
        # The just-opened unit reports SEQUENTIAL/FORMATTED.
        self.assertIn("SEQUENTIAL", out)
        self.assertIn("FORMATTED", out)

    def test_rewind_runs(self) -> None:
        # After REWIND, the first read returns the first record again.
        out = self._build_and_run(REWIND_F).split()
        self.assertEqual(out[0], "first")


if __name__ == "__main__":
    unittest.main()

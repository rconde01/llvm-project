"""Comprehensive Fortran file-I/O behavior tests.

Where ``test_file_io.py`` covers the I/O paths the SPICE corpus happens to
exercise, this file pins down the *Fortran-standard behavior* of OPEN /
CLOSE / REWIND / BACKSPACE / ENDFILE / INQUIRE across their modes and
options -- STATUS, ACCESS, FORM, RECL, POSITION, IOSTAT, and CLOSE STATUS --
so the runtime stays consistent with a real Fortran compiler and
regressions are caught early.  Each test converts a small program, compiles
it with the runtime headers, runs it, and checks the observable behavior.
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


def _run(src: str) -> str:
    """Convert, compile and run ``src``; return its stdout.  Runs in a fresh
    temp dir so file operations don't collide between tests."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "in.f"
        f.write_text(src)
        cpp = Path(d) / "out.cpp"
        cpp.write_text(convert_file(f))
        exe = Path(d) / "out"
        cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        assert cxx is not None
        comp = subprocess.run(
            [cxx, "-std=c++20", "-I", str(RUNTIME_INCLUDE), str(cpp), "-o", str(exe)],
            capture_output=True, text=True, check=False,
        )
        if comp.returncode != 0:
            raise AssertionError(f"compile failed:\n{comp.stderr}\n{cpp.read_text()}")
        run = subprocess.run(
            [str(exe)], capture_output=True, text=True, check=False, cwd=d
        )
        assert run.returncode == 0, f"run failed: {run.stderr}\n{cpp.read_text()}"
        return run.stdout


@unittest.skipUnless(_have_flang() and _have_cxx(), "need flang and a C++20 compiler")
class OpenStatusModeTests(unittest.TestCase):
    """STATUS = OLD / NEW / REPLACE / SCRATCH / UNKNOWN."""

    def test_replace_truncates_existing(self) -> None:
        # STATUS='REPLACE' on an existing file discards the old contents.
        out = _run("""\
      program p
      integer k
      open(10, file='r.dat', status='replace')
      write(10,*) 111
      write(10,*) 222
      close(10)
      open(10, file='r.dat', status='replace')
      write(10,*) 999
      close(10)
      open(10, file='r.dat', status='old')
      read(10,*) k
      close(10)
      write(*,*) k
      end
""")
        self.assertEqual(out.split(), ["999"])

    def test_new_creates_then_old_reads(self) -> None:
        out = _run("""\
      program p
      integer k
      open(10, file='n.dat', status='new')
      write(10,*) 7
      close(10)
      open(10, file='n.dat', status='old')
      read(10,*) k
      close(10)
      write(*,*) k
      end
""")
        self.assertEqual(out.split(), ["7"])

    def test_new_on_existing_sets_iostat(self) -> None:
        out = _run("""\
      program p
      integer ios
      open(10, file='e.dat', status='replace')
      close(10)
      open(11, file='e.dat', status='new', iostat=ios)
      if (ios .ne. 0) then
         write(*,*) 'FAILED'
      else
         write(*,*) 'OPENED'
      end if
      end
""")
        self.assertEqual(out.split(), ["FAILED"])

    def test_old_missing_sets_iostat(self) -> None:
        out = _run("""\
      program p
      integer ios
      open(10, file='absent.dat', status='old', iostat=ios)
      if (ios .ne. 0) then
         write(*,*) 'FAILED'
      else
         write(*,*) 'OPENED'
      end if
      end
""")
        self.assertEqual(out.split(), ["FAILED"])

    def test_unknown_creates_and_writes(self) -> None:
        # Default STATUS='UNKNOWN' on a missing file creates it.
        out = _run("""\
      program p
      integer k
      open(10, file='u.dat')
      write(10,*) 55
      close(10)
      open(10, file='u.dat', status='old')
      read(10,*) k
      close(10)
      write(*,*) k
      end
""")
        self.assertEqual(out.split(), ["55"])

    def test_scratch_write_read_roundtrip(self) -> None:
        # A SCRATCH file has no name; it must still back writes and reads.
        out = _run("""\
      program p
      integer a, b
      open(10, status='scratch')
      write(10,*) 3
      write(10,*) 4
      rewind(10)
      read(10,*) a
      read(10,*) b
      close(10)
      write(*,*) a, b
      end
""")
        self.assertEqual(out.split(), ["3", "4"])


@unittest.skipUnless(_have_flang() and _have_cxx(), "need flang and a C++20 compiler")
class CloseStatusTests(unittest.TestCase):
    def test_close_delete_removes_file(self) -> None:
        # CLOSE(STATUS='DELETE') removes the file; a later OLD open fails.
        out = _run("""\
      program p
      integer ios
      open(10, file='d.dat', status='replace')
      write(10,*) 1
      close(10, status='delete')
      open(11, file='d.dat', status='old', iostat=ios)
      if (ios .ne. 0) then
         write(*,*) 'GONE'
      else
         write(*,*) 'PRESENT'
      end if
      end
""")
        self.assertEqual(out.split(), ["GONE"])

    def test_close_keep_retains_file(self) -> None:
        out = _run("""\
      program p
      integer ios
      open(10, file='k.dat', status='replace')
      write(10,*) 1
      close(10, status='keep')
      open(11, file='k.dat', status='old', iostat=ios)
      if (ios .eq. 0) then
         write(*,*) 'PRESENT'
      else
         write(*,*) 'GONE'
      end if
      close(11)
      end
""")
        self.assertEqual(out.split(), ["PRESENT"])


@unittest.skipUnless(_have_flang() and _have_cxx(), "need flang and a C++20 compiler")
class PositioningTests(unittest.TestCase):
    def test_rewind_rereads_from_start(self) -> None:
        out = _run("""\
      program p
      integer a, b
      open(10, file='rw.dat', status='replace')
      write(10,*) 100
      write(10,*) 200
      rewind(10)
      read(10,*) a
      read(10,*) b
      close(10)
      write(*,*) a, b
      end
""")
        self.assertEqual(out.split(), ["100", "200"])

    def test_backspace_rereads_last_record(self) -> None:
        out = _run("""\
      program p
      integer a, b, c
      open(10, file='bs.dat', status='replace')
      write(10,*) 10
      write(10,*) 20
      rewind(10)
      read(10,*) a
      read(10,*) b
      backspace(10)
      read(10,*) c
      close(10)
      write(*,*) a, b, c
      end
""")
        self.assertEqual(out.split(), ["10", "20", "20"])


@unittest.skipUnless(_have_flang() and _have_cxx(), "need flang and a C++20 compiler")
class AccessAndFormTests(unittest.TestCase):
    def test_direct_access_records_out_of_order(self) -> None:
        # ACCESS='DIRECT' with REC= writes/reads fixed-length records by
        # index, in any order.
        out = _run("""\
      program p
      integer a, b, c
      open(10, file='dir.dat', access='direct', recl=4,
     .     form='unformatted', status='replace')
      write(10, rec=3) 30
      write(10, rec=1) 10
      write(10, rec=2) 20
      read(10, rec=2) b
      read(10, rec=1) a
      read(10, rec=3) c
      close(10)
      write(*,*) a, b, c
      end
""")
        self.assertEqual(out.split(), ["10", "20", "30"])

    def test_unformatted_sequential_roundtrip(self) -> None:
        out = _run("""\
      program p
      double precision x, y
      open(10, file='uf.dat', form='unformatted', status='replace')
      write(10) 1.5d0
      write(10) 2.5d0
      rewind(10)
      read(10) x
      read(10) y
      close(10)
      write(*,*) x, y
      end
""")
        self.assertEqual(out.split(), ["1.5", "2.5"])


    def test_position_append_extends_file(self) -> None:
        # POSITION='APPEND' opens at end-of-file, so writes add to the
        # existing content rather than overwriting it.
        out = _run("""\
      program p
      integer a, b, c
      open(10, file='ap.dat', status='replace')
      write(10,*) 1
      write(10,*) 2
      close(10)
      open(10, file='ap.dat', status='old', position='append')
      write(10,*) 3
      close(10)
      open(10, file='ap.dat', status='old')
      read(10,*) a
      read(10,*) b
      read(10,*) c
      close(10)
      write(*,*) a, b, c
      end
""")
        self.assertEqual(out.split(), ["1", "2", "3"])

    def test_endfile_truncates(self) -> None:
        # ENDFILE marks EOF at the current position: after writing three
        # records, rewinding, reading one and ENDFILE, only one record
        # remains when the file is re-read.
        out = _run("""\
      program p
      integer x, n
      open(10, file='ef.dat', status='replace')
      write(10,*) 11
      write(10,*) 22
      write(10,*) 33
      rewind(10)
      read(10,*) x
      endfile(10)
      close(10)
      open(10, file='ef.dat', status='old')
      n = 0
  1   read(10,*,end=9) x
      n = n + 1
      goto 1
  9   close(10)
      write(*,*) n
      end
""")
        self.assertEqual(out.split(), ["1"])


@unittest.skipUnless(_have_flang() and _have_cxx(), "need flang and a C++20 compiler")
class InquireTests(unittest.TestCase):
    def test_exist_before_and_after_create(self) -> None:
        out = _run("""\
      program p
      logical ex1, ex2
      inquire(file='iq.dat', exist=ex1)
      open(10, file='iq.dat', status='replace')
      close(10)
      inquire(file='iq.dat', exist=ex2)
      write(*,*) ex1, ex2
      end
""")
        # Fortran logicals print as F / T.
        toks = out.split()
        self.assertEqual(toks[0].upper()[0], "F")
        self.assertEqual(toks[1].upper()[0], "T")

    def test_opened_and_number(self) -> None:
        out = _run("""\
      program p
      logical op
      integer num
      open(17, file='op.dat', status='replace')
      inquire(unit=17, opened=op)
      inquire(file='op.dat', number=num)
      close(17)
      write(*,*) op, num
      end
""")
        toks = out.split()
        self.assertEqual(toks[0].upper()[0], "T")
        self.assertEqual(toks[1], "17")


if __name__ == "__main__":
    unittest.main()

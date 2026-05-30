"""Tests for GOTO elimination into structured control flow.

The structuring pass turns goto/labels into ordinary control flow with no
``goto`` keyword: the forward ``if (c) goto L`` skip becomes an ``if``
block, and anything it can't fold (backward jumps, etc.) becomes a
goto-free ``while``/``switch`` dispatch loop.
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


SKIP_F = """\
      program skip
      n = 3
      if (n .gt. 0) goto 20
      n = 999
   20 continue
      print *, n
      end
"""

LOOP_F = """\
      program lp
      n = 0
   10 n = n + 1
      if (n .lt. 5) goto 10
      if (n .eq. 5) goto 30
      n = -1
   30 continue
      print *, n
      end
"""

COMPUTED_F = """\
      program cg
      i = 2
      goto (10,20,30), i
   10 k = 1
      goto 99
   20 k = 2
      goto 99
   30 k = 3
   99 continue
      print *, k
      end
"""

ARITH_F = """\
      program ar
      x = 2.0
      if (x) 40,50,60
   40 k = -1
      goto 99
   50 k = 0
      goto 99
   60 k = 1
   99 continue
      print *, k
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


def _run(src: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        cpp = Path(d) / "out.cpp"
        cpp.write_text(_convert(src))
        exe = Path(d) / "out"
        cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        assert cxx is not None
        comp = subprocess.run(
            [cxx, "-std=c++20", "-I", str(RUNTIME_INCLUDE), str(cpp), "-o", str(exe)],
            capture_output=True, text=True, check=False,
        )
        if comp.returncode != 0:
            raise AssertionError(f"compile failed:\n{comp.stderr}\n{cpp.read_text()}")
        run = subprocess.run([str(exe)], capture_output=True, text=True, check=False)
        assert run.returncode == 0, run.stderr
        return run.stdout


@unittest.skipUnless(_have_flang(), "flang binary not available")
class GotoEmitTests(unittest.TestCase):
    def test_no_goto_keyword(self) -> None:
        for src in (SKIP_F, LOOP_F, COMPUTED_F, ARITH_F):
            cpp = _convert(src)
            self.assertNotIn("goto", cpp)
            self.assertNotIn("does not yet translate", cpp)

    def test_forward_skip_becomes_if(self) -> None:
        cpp = _convert(SKIP_F)
        self.assertIn("if (!(n > 0)) {", cpp)
        self.assertNotIn("_pc", cpp)  # no dispatch loop needed

    def test_backward_loop_uses_dispatch(self) -> None:
        cpp = _convert(LOOP_F)
        # Can't fold a backward jump; falls back to the dispatch loop.
        self.assertIn("int _pc", cpp)
        self.assertIn("while (_pc != ", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class GotoRunTests(unittest.TestCase):
    def test_forward_skip_runs(self) -> None:
        self.assertEqual(_run(SKIP_F).split(), ["3"])

    def test_backward_loop_runs(self) -> None:
        self.assertEqual(_run(LOOP_F).split(), ["5"])

    def test_computed_goto_runs(self) -> None:
        self.assertEqual(_run(COMPUTED_F).split(), ["2"])

    def test_arithmetic_if_runs(self) -> None:
        self.assertEqual(_run(ARITH_F).split(), ["1"])


if __name__ == "__main__":
    unittest.main()

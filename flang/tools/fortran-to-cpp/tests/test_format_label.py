"""Tests for label-referenced FORMAT statements (``write(u, 100) ...``)."""

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


FMT_F = """\
      program fmt
      x = 3.14159
      n = 42
      write(6, 100) n, x
  100 format(1x, 'n=', i3, ' x=', f6.3)
      end
"""

# Label referenced before it is defined, and reused by two writes.
FMT_FORWARD_F = """\
      program fwd
      i = 1
      j = 2
      write(6, 10) i
      write(6, 10) j
   10 format('v=', i2)
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
class FormatLabelEmitTests(unittest.TestCase):
    def test_label_resolved_to_format(self) -> None:
        cpp = _convert(FMT_F)
        # The labelled FORMAT is resolved like an inline format string.
        self.assertIn('std::format("{:3d}", n)', cpp)
        self.assertIn('std::format("{:#6.3f}", x)', cpp)

    def test_format_statement_dropped(self) -> None:
        cpp = _convert(FMT_F)
        self.assertNotIn("does not yet translate", cpp)
        self.assertNotIn("FormatStmt", cpp)

    def test_forward_and_shared_label(self) -> None:
        cpp = _convert(FMT_FORWARD_F)
        # Both writes resolve the same (later-defined) label.
        self.assertEqual(cpp.count('std::format("{:2d}"'), 2)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class FormatLabelRunTests(unittest.TestCase):
    def _run(self, src: str) -> str:
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
                [str(exe)], capture_output=True, text=True, check=False
            )
            self.assertEqual(run.returncode, 0, msg=run.stderr)
            return run.stdout

    def test_format_runs(self) -> None:
        # (1x,'n=',i3,' x=',f6.3) with n=42, x=3.14159.
        self.assertEqual(self._run(FMT_F).rstrip("\n"), " n= 42 x= 3.142")


if __name__ == "__main__":
    unittest.main()

"""Command-line argument intrinsics (IARGC / GETARG).

The toolkit's CLI programs read their arguments through these vendor
intrinsics; the runtime serves them from a process-wide store populated by
the generated ``main(argc, argv)``.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from _support import RUNTIME_INCLUDE, have_cxx, have_flang
from converter.project import convert_files


ARGS_F = """\
      program p
      character*32 arg
      integer i, n, st
      n = iargc()
      write(*,*) n
      do i = 1, n
         call getarg(i, arg, st)
         write(*,*) arg
      end do
      end
"""


@unittest.skipUnless(have_flang(), "flang binary not available")
class CommandLineEmitTests(unittest.TestCase):
    def _convert(self, src: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "in.f"
            p.write_text(src)
            return "\n".join(convert_files([p]).values())

    def test_intrinsics_and_main_signature(self) -> None:
        cpp = self._convert(ARGS_F)
        self.assertIn("fortran::iargc()", cpp)
        self.assertIn("fortran::getarg(", cpp)
        self.assertIn("int main(int argc, char** argv)", cpp)
        self.assertIn("fortran::set_command_args(argc, argv)", cpp)


@unittest.skipUnless(have_flang() and have_cxx(), "need flang and a C++20 compiler")
class CommandLineRunTests(unittest.TestCase):
    def test_reads_command_arguments(self) -> None:
        cxx = (
            __import__("shutil").which("c++")
            or __import__("shutil").which("g++")
            or __import__("shutil").which("clang++")
        )
        assert cxx is not None
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "in.f"
            sp.write_text(ARGS_F)
            outputs = convert_files([sp])
            main_cpp = None
            for name, text in outputs.items():
                if Path(name).suffix in (".hpp", ".h"):
                    (Path(d) / Path(name).name).write_text(text)
                else:
                    main_cpp = Path(d) / "out.cpp"
                    main_cpp.write_text(text)
            assert main_cpp is not None
            exe = Path(d) / "prog"
            comp = subprocess.run(
                [cxx, "-std=c++20", "-I", str(RUNTIME_INCLUDE), "-I", str(d),
                 str(main_cpp), "-o", str(exe)],
                capture_output=True, text=True,
            )
            self.assertEqual(comp.returncode, 0, comp.stderr)
            run = subprocess.run([str(exe), "alpha", "beta"],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            out = run.stdout.split()
            # iargc() == 2, then the two arguments.
            self.assertEqual(out[0], "2")
            self.assertIn("alpha", run.stdout)
            self.assertIn("beta", run.stdout)


if __name__ == "__main__":
    unittest.main()

"""Tests for multi-file projects with inter-module ``USE`` dependencies.

A file that ``USE``s a module can only be semantically analyzed once the
defining file's ``.mod`` exists; :func:`converter.convert_files` resolves
the file order from the module graph and shares one module directory so
each file's modules are available to its dependents.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import subprocess

from converter import convert_files
from converter.project import SHARED_HEADER_NAME, _dependency_order


def _have_cxx() -> bool:
    return any(shutil.which(n) for n in ("c++", "g++", "clang++"))


RUNTIME_INCLUDE = Path(__file__).resolve().parent.parent / "runtime" / "include"


def _have_flang() -> bool:
    return bool(
        os.environ.get("FLANG") or shutil.which("flang-new") or shutil.which("flang")
    )


CONSTANTS = """\
module geo_constants
  implicit none
  real, parameter :: pi = 3.14159265
end module geo_constants
"""

# Deliberately listed/created before the module it depends on, to prove the
# converter reorders by dependency rather than trusting argument order.
USER = """\
module circle
  use geo_constants
  implicit none
contains
  real function area(r)
    real, intent(in) :: r
    area = pi * r * r
  end function area
end module circle
"""

MAIN = """\
program p
  use circle
  implicit none
  print *, area(2.0)
end program p
"""


@unittest.skipUnless(_have_flang(), "flang binary not available")
class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flang = os.environ.get("FLANG")
        self.dir = Path(tempfile.mkdtemp())
        self.consts = self.dir / "geo_constants.f90"
        self.circle = self.dir / "circle.f90"
        self.main = self.dir / "main.f90"
        self.consts.write_text(CONSTANTS)
        self.circle.write_text(USER)
        self.main.write_text(MAIN)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_dependency_order_puts_providers_first(self) -> None:
        # Pass them out of order; providers must come before dependents.
        order = _dependency_order(
            [self.main, self.circle, self.consts], flang=self.flang
        )
        self.assertLess(order.index(self.consts), order.index(self.circle))
        self.assertLess(order.index(self.circle), order.index(self.main))

    def test_dependent_file_converts(self) -> None:
        # Converting circle.f90 alone would fail sema (no geo_constants.mod);
        # via convert_files it succeeds because the provider runs first.
        results = convert_files(
            [self.circle, self.consts, self.main], flang=self.flang
        )
        # Three sources plus the shared header.
        self.assertEqual(len(results), 4)
        self.assertIn(Path(SHARED_HEADER_NAME), results)
        circle_cpp = results[self.circle]
        self.assertIn("area", circle_cpp)
        self.assertNotIn("TODO", circle_cpp)
        # ``pi`` is a module PARAMETER (compile-time constant): the shared
        # header exposes it as a free ``inline constexpr`` referenced
        # without any threaded module instance.
        self.assertIn(
            "inline constexpr float pi", results[Path(SHARED_HEADER_NAME)]
        )
        self.assertIn("pi", circle_cpp)

    def test_unparseable_file_is_skipped_not_fatal(self) -> None:
        # A file flang can't parse must not abort the whole project: the
        # good files still convert, the bad one is just skipped.
        bad = self.dir / "bad.f90"
        bad.write_text("this is not fortran @@@ !!!\n")
        results = convert_files(
            [self.consts, self.circle, bad], flang=self.flang
        )
        self.assertIn(self.consts, results)
        self.assertIn(self.circle, results)
        self.assertNotIn(bad, results)  # skipped
        self.assertIn(Path(SHARED_HEADER_NAME), results)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ProjectCompileTests(unittest.TestCase):
    def test_project_compiles_and_runs(self) -> None:
        flang = os.environ.get("FLANG")
        with tempfile.TemporaryDirectory() as d:
            dpath = Path(d)
            (dpath / "geo_constants.f90").write_text(CONSTANTS)
            (dpath / "circle.f90").write_text(USER)
            (dpath / "main.f90").write_text(MAIN)
            sources = [
                dpath / "main.f90",
                dpath / "circle.f90",
                dpath / "geo_constants.f90",
            ]
            out = dpath / "out"
            out.mkdir()
            results = convert_files(sources, flang=flang)
            cpp_files = []
            for key, text in results.items():
                name = key.name if key.suffix in (".hpp", ".h") else key.stem + ".cpp"
                (out / name).write_text(text)
                if name.endswith(".cpp"):
                    cpp_files.append(str(out / name))
            cxx = (
                shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
            )
            assert cxx is not None
            exe = out / "prog"
            comp = subprocess.run(
                [cxx, "-std=c++20", "-I", str(RUNTIME_INCLUDE), "-I", str(out),
                 *cpp_files, "-o", str(exe)],
                capture_output=True, text=True, check=False,
            )
            if comp.returncode != 0:
                self.fail(f"compile failed:\n{comp.stderr}")
            run = subprocess.run(
                [str(exe)], capture_output=True, text=True, check=False
            )
            self.assertEqual(run.returncode, 0, msg=run.stderr)
            # pi * 2 * 2 == 12.566...
            self.assertTrue(run.stdout.strip().startswith("12.56"), run.stdout)


if __name__ == "__main__":
    unittest.main()

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

from converter import convert_files
from converter.project import _dependency_order


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
        self.assertEqual(len(results), 3)
        circle_cpp = results[self.circle]
        self.assertIn("area", circle_cpp)
        self.assertNotIn("TODO", circle_cpp)


if __name__ == "__main__":
    unittest.main()

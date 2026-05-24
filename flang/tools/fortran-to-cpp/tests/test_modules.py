"""Tests for module variables (the third D2.b state category)."""

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


CONFIG_F90 = """\
module config
  real :: gravity = 9.8
  integer :: count
contains
  subroutine bump()
    count = count + 1
  end subroutine
end module

program demo
  use config
  call bump()
  call bump()
  print *, gravity, count
end program
"""


COUNTERS_F90 = """\
module counters
  integer :: hits = 0
end module

subroutine record()
  use counters
  hits = hits + 1
end subroutine

subroutine report()
  use counters
  print *, "hits:", hits
end subroutine

program demo
  call record()
  call record()
  call record()
  call report()
end program
"""


def _convert(src: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".f90", delete=False, encoding="utf-8"
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        return convert_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


@unittest.skipUnless(_have_flang(), "flang binary not available")
class ModuleEmitTests(unittest.TestCase):
    def test_module_vars_become_struct(self) -> None:
        cpp = _convert(CONFIG_F90)
        self.assertIn("struct ConfigModule {", cpp)
        # Initializer preserved.
        self.assertIn("float gravity = 9.8f;", cpp)
        self.assertIn("std::int32_t count{};", cpp)

    def test_module_procedure_takes_module_state(self) -> None:
        cpp = _convert(CONFIG_F90)
        self.assertIn("void bump(ConfigModule& config_module)", cpp)
        self.assertIn("config_module.count = config_module.count + 1;", cpp)

    def test_using_program_owns_instance(self) -> None:
        cpp = _convert(CONFIG_F90)
        self.assertNotIn("void demo(ConfigModule", cpp)
        self.assertIn("ConfigModule config_module", cpp)
        self.assertIn("config_module.gravity", cpp)

    def test_main_without_use_still_threads_state(self) -> None:
        cpp = _convert(COUNTERS_F90)
        # record/report take the module struct...
        self.assertIn("void record(CountersModule& counters_module)", cpp)
        self.assertIn("void report(CountersModule& counters_module)", cpp)
        # ...and main allocates and passes it even though it doesn't
        # ``use`` the module itself.
        self.assertIn("CountersModule counters_module", cpp)
        self.assertIn("record(counters_module);", cpp)


@unittest.skipUnless(
    _have_flang() and _have_cxx(), "need flang and a C++20 compiler"
)
class ModuleRunTests(unittest.TestCase):
    def _run(self, src: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "in.f90"
            f.write_text(src)
            cpp = Path(d) / "out.cpp"
            cpp.write_text(convert_file(f))
            exe = Path(d) / "out"
            cxx = (
                shutil.which("c++")
                or shutil.which("g++")
                or shutil.which("clang++")
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

    def test_config_runs(self) -> None:
        out = self._run(CONFIG_F90)
        # gravity untouched (9.8), count bumped twice.
        self.assertIn("9.8", out)
        self.assertIn("2", out)

    def test_counters_runs(self) -> None:
        out = self._run(COUNTERS_F90)
        self.assertIn("hits: 3", out)


if __name__ == "__main__":
    unittest.main()

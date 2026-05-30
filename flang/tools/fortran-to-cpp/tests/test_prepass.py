"""Tests for the source sanitizing prepass (control-character removal)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from converter import convert_file
from converter.prepass import needs_sanitizing, sanitize_bytes, sanitized_source


def _have_flang() -> bool:
    return bool(
        os.environ.get("FLANG") or shutil.which("flang-new") or shutil.which("flang")
    )


class SanitizeUnitTests(unittest.TestCase):
    def test_control_byte_detected_and_replaced(self) -> None:
        raw = b"      x = 1\x1a\n"  # stray DOS ^Z
        self.assertTrue(needs_sanitizing(raw))
        self.assertEqual(sanitize_bytes(raw), b"      x = 1 \n")

    def test_whitespace_controls_preserved(self) -> None:
        raw = b"a\tb\r\n\f"  # tab, CR, LF, form-feed all kept
        self.assertFalse(needs_sanitizing(raw))
        self.assertEqual(sanitize_bytes(raw), raw)

    def test_non_ascii_left_untouched(self) -> None:
        # flang tolerates non-ASCII (e.g. a curly apostrophe); don't touch it.
        raw = "C user’s note\n".encode("utf-8")
        self.assertFalse(needs_sanitizing(raw))
        self.assertEqual(sanitize_bytes(raw), raw)

    def test_context_manager_yields_original_when_clean(self) -> None:
        with tempfile.NamedTemporaryFile(
            "wb", suffix=".f", delete=False
        ) as f:
            f.write(b"      end\n")
            p = Path(f.name)
        try:
            with sanitized_source(p) as out:
                self.assertEqual(out, p)  # unchanged -> same path
        finally:
            p.unlink(missing_ok=True)

    def test_context_manager_makes_temp_when_dirty(self) -> None:
        with tempfile.NamedTemporaryFile(
            "wb", suffix=".f", delete=False
        ) as f:
            f.write(b"      end\n\x1a")
            p = Path(f.name)
        try:
            with sanitized_source(p) as out:
                self.assertNotEqual(out, p)
                self.assertEqual(out.suffix, ".f")  # form-detection preserved
                self.assertNotIn(b"\x1a", out.read_bytes())
            self.assertFalse(out.exists())  # cleaned up on exit
        finally:
            p.unlink(missing_ok=True)


@unittest.skipUnless(_have_flang(), "flang binary not available")
class SanitizeConvertTests(unittest.TestCase):
    def test_convert_file_with_control_byte(self) -> None:
        # A ^Z byte that flang would otherwise reject ("bad character").
        src = (
            "      program p\n"
            "      integer :: x\n"
            "      x = 41 + 1\n"
            "      print *, x\n"
            "\x1a\n"
            "      end\n"
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".f", delete=False, encoding="latin-1"
        ) as f:
            f.write(src)
            p = Path(f.name)
        try:
            cpp = convert_file(p)  # must not raise
            self.assertIn("int main", cpp)
            self.assertIn("x = 41 + 1", cpp)
        finally:
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

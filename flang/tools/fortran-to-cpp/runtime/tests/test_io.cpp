//===-- test_io.cpp - Tests for fortran::io format helpers ------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "fortran/io.hpp"
#include "test_main.hpp"

#include <string>
#include <string_view>

using namespace std::string_view_literals;

// ---- G edit descriptor ---------------------------------------------------

TEST(fmt_G_uses_f_format_for_in_range) {
  // For G12.5, a value of 1.234 should render in F-format.
  auto s = fortran::io::fmt_G(1.234, 12, 5);
  // 12-wide; trailing blanks for the absent exponent.  Exact column
  // counts depend on the standard's k-rule; check the rendered value
  // appears and the result is the requested width.
  CHECK_EQ(s.size(), 12u);
  CHECK(s.find("1.") != std::string::npos);
}

TEST(fmt_G_uses_e_format_for_out_of_range) {
  // For G12.5, 1.0e10 is well outside [0.1, 10^5).
  auto s = fortran::io::fmt_G(1.0e10, 12, 5);
  CHECK(s.size() == 12u);
  CHECK(s.find('e') != std::string::npos);
}

TEST(fmt_G_handles_zero) {
  auto s = fortran::io::fmt_G(0.0, 10, 4);
  CHECK_EQ(s.size(), 10u);
}

// ---- P scale factor ------------------------------------------------------

TEST(fmt_F_with_scale_shifts_decimal) {
  // 1P, F8.3 of 0.123 -> 1.230   (value * 10^1)
  auto s = fortran::io::fmt_F_with_scale(0.123, 1, 8, 3);
  CHECK_EQ(s.size(), 8u);
  // The displayed value is 1.230 — verify the first non-blank chars.
  CHECK(s.find("1.230") != std::string::npos);
}

// ---- Tab / pad helpers --------------------------------------------------

TEST(pad_to_advances_to_column) {
  std::string buf = "abc";
  fortran::io::pad_to(buf, 8);                  // advance to column 8
  CHECK_EQ(buf, "abc    "sv);
  CHECK_EQ(buf.size(), 7u);                     // column 8 == position 7
}

TEST(pad_to_does_nothing_when_already_past) {
  std::string buf = "abcdefgh";
  fortran::io::pad_to(buf, 4);                  // already past col 4
  CHECK_EQ(buf, "abcdefgh"sv);
}

TEST(skip_emits_blanks) {
  CHECK_EQ(fortran::io::skip(3), "   "sv);
  CHECK_EQ(fortran::io::skip(0), ""sv);
}

// ---- Sign control --------------------------------------------------------

TEST(force_sign_emits_plus_for_positive) {
  CHECK_EQ(fortran::io::fmt_int_force_sign(42, 5), "  +42"sv);
  CHECK_EQ(fortran::io::fmt_int_force_sign(-3, 5), "   -3"sv);
}

TEST(no_sign_format_matches_default_d) {
  CHECK_EQ(fortran::io::fmt_int_no_sign(42, 5), "   42"sv);
  CHECK_EQ(fortran::io::fmt_int_no_sign(-3, 5), "   -3"sv);
}

FORTRAN_RT_TEST_MAIN()

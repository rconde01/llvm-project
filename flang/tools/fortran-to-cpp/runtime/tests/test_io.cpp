//===-- test_io.cpp - Tests for ftn::io format helpers ------*- C++ -*-===//
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
  auto s = ftn::io::fmt_G(1.234, 12, 5);
  // 12-wide; trailing blanks for the absent exponent.  Exact column
  // counts depend on the standard's k-rule; check the rendered value
  // appears and the result is the requested width.
  CHECK_EQ(s.size(), 12u);
  CHECK(s.find("1.") != std::string::npos);
}

TEST(fmt_G_uses_e_format_for_out_of_range) {
  // For G12.5, 1.0e10 is well outside [0.1, 10^5).
  auto s = ftn::io::fmt_G(1.0e10, 12, 5);
  CHECK(s.size() == 12u);
  CHECK(s.find('e') != std::string::npos);
}

TEST(fmt_G_handles_zero) {
  auto s = ftn::io::fmt_G(0.0, 10, 4);
  CHECK_EQ(s.size(), 10u);
}

// ---- E edit descriptor ---------------------------------------------------

TEST(fmt_E_fortran_style_mantissa) {
  // Fortran E12.4 of 3.14159 -> "  0.3142E+01" (mantissa in [0.1,1)).
  auto s = ftn::io::fmt_E(3.14159, 12, 4);
  CHECK_EQ(s, "  0.3142E+01"sv);
}

TEST(fmt_E_negative) {
  auto s = ftn::io::fmt_E(-3.14159, 12, 4);
  CHECK_EQ(s, " -0.3142E+01"sv);
}

TEST(fmt_E_negative_exponent) {
  auto s = ftn::io::fmt_E(0.0123, 12, 4);
  CHECK_EQ(s, "  0.1230E-01"sv);
}

TEST(fmt_E_zero) {
  auto s = ftn::io::fmt_E(0.0, 12, 4);
  CHECK_EQ(s, "  0.0000E+00"sv);
}

// ---- P scale factor ------------------------------------------------------

TEST(fmt_F_with_scale_shifts_decimal) {
  // 1P, F8.3 of 0.123 -> 1.230   (value * 10^1)
  auto s = ftn::io::fmt_F_with_scale(0.123, 1, 8, 3);
  CHECK_EQ(s.size(), 8u);
  // The displayed value is 1.230 — verify the first non-blank chars.
  CHECK(s.find("1.230") != std::string::npos);
}

// ---- Tab / pad helpers --------------------------------------------------

TEST(pad_to_advances_to_column) {
  std::string buf = "abc";
  ftn::io::pad_to(buf, 8);                  // advance to column 8
  CHECK_EQ(buf, "abc    "sv);
  CHECK_EQ(buf.size(), 7u);                     // column 8 == position 7
}

TEST(pad_to_does_nothing_when_already_past) {
  std::string buf = "abcdefgh";
  ftn::io::pad_to(buf, 4);                  // already past col 4
  CHECK_EQ(buf, "abcdefgh"sv);
}

TEST(skip_emits_blanks) {
  CHECK_EQ(ftn::io::skip(3), "   "sv);
  CHECK_EQ(ftn::io::skip(0), ""sv);
}

// ---- Sign control --------------------------------------------------------

TEST(force_sign_emits_plus_for_positive) {
  CHECK_EQ(ftn::io::fmt_int_force_sign(42, 5), "  +42"sv);
  CHECK_EQ(ftn::io::fmt_int_force_sign(-3, 5), "   -3"sv);
}

TEST(no_sign_format_matches_default_d) {
  CHECK_EQ(ftn::io::fmt_int_no_sign(42, 5), "   42"sv);
  CHECK_EQ(ftn::io::fmt_int_no_sign(-3, 5), "   -3"sv);
}

TEST(units_preconnected_streams) {
  ftn::io::Units u;
  CHECK_EQ(&u.out(6), &std::cout);
  CHECK_EQ(&u.out(0), &std::cerr);
  CHECK_EQ(&u.in(5), &std::cin);
}

TEST(units_file_round_trip) {
  const std::string path{"fc_units_test.dat"};
  {
    ftn::io::Units u;
    u.open(10, path, "replace");
    u.out(10) << 7 << ' ' << 2.5 << '\n';
    u.close(10);
  }
  {
    ftn::io::Units u;
    u.open(11, path, "old");
    int k = 0;
    double x = 0.0;
    u.in(11) >> k >> x;
    u.close(11);
    CHECK_EQ(k, 7);
    CHECK(x > 2.49 && x < 2.51);
  }
  std::remove(path.c_str());
}

FORTRAN_RT_TEST_MAIN()

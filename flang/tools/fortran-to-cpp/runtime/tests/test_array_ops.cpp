//===-- test_array_ops.cpp - Tests for elementwise array operators -*- C++ -*-//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "fortran/array.hpp"
#include "fortran/array_ops.hpp"
#include "fortran/array_ref.hpp"
#include "test_main.hpp"

#include <type_traits>

using namespace fortran;

static Array<float, 1> iota4() {
  Array<float, 1> a({4});
  for (index_t i = 1; i <= 4; ++i) {
    a(i) = static_cast<float>(i); // 1,2,3,4
  }
  return a;
}

TEST(array_array_arithmetic) {
  auto a = iota4();
  Array<float, 1> b({4});
  b = 10.0f;
  auto s = a + b; // 11,12,13,14
  CHECK_EQ(s(1), 11.0f);
  CHECK_EQ(s(4), 14.0f);
  auto d = b - a; // 9,8,7,6
  CHECK_EQ(d(1), 9.0f);
  CHECK_EQ(d(4), 6.0f);
}

TEST(array_scalar_both_sides) {
  auto a = iota4();
  auto c = a * 2.0f; // 2,4,6,8
  CHECK_EQ(c(3), 6.0f);
  auto e = 12.0f / a; // 12,6,4,3
  CHECK_EQ(e(2), 6.0f);
  CHECK_EQ(e(4), 3.0f);
}

TEST(section_operands) {
  // ArrayRef (x) ArrayRef — sections are non-owning views.
  auto a = iota4();
  Array<float, 1> b({4});
  b = 10.0f;
  auto diff = a.section(2, 4) - b.section(2, 4); // (2,3,4)-(10,10,10)
  CHECK_EQ(diff(1), -8.0f);
  CHECK_EQ(diff(3), -6.0f);
  // Then divide the resulting Array by a scalar.
  auto q = (a.section(2, 4) - b.section(2, 4)) / 2.0f;
  CHECK_EQ(q(2), -3.5f);
}

TEST(mixed_element_types_promote) {
  auto a = iota4(); // float
  Array<double, 1> e({4});
  e = 0.5;
  auto m = a + e; // common_type<float,double> == double
  static_assert(std::is_same_v<decltype(m)::value_type, double>);
  CHECK_EQ(m(1), 1.5);
  CHECK_EQ(m(4), 4.5);
}

TEST(comparison_yields_bool_array) {
  auto a = iota4();
  auto mask = a > 2.0f; // F,F,T,T
  static_assert(std::is_same_v<decltype(mask)::value_type, bool>);
  CHECK(!mask(2));
  CHECK(mask(3));
  auto eq = a == 3.0f;
  CHECK(eq(3));
  CHECK(!eq(1));
}

TEST(rank2_keeps_shape_and_bounds) {
  Array<int, 2> m({{-1, 0}, {2, 3}}); // lower (-1,0), extents (2,3)
  m = 1;
  auto r = m + m;
  CHECK_EQ(r.lbound(1), -1);
  CHECK_EQ(r.lbound(2), 0);
  CHECK_EQ(r.extent(1), 2);
  CHECK_EQ(r.extent(2), 3);
  CHECK_EQ(r(-1, 0), 2);
}

FORTRAN_RT_TEST_MAIN()

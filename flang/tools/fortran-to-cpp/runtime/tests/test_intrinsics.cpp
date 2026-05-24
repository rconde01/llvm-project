//===-- test_intrinsics.cpp - Tests for array intrinsics --------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "fortran/array.hpp"
#include "fortran/array_ref.hpp"
#include "fortran/intrinsics.hpp"
#include "test_main.hpp"

using fortran::Array;
using fortran::ArrayRef;

static Array<int, 1> make_squares() {
  Array<int, 1> a({5});
  for (int i = 1; i <= 5; ++i) {
    a(i) = i * i; // 1, 4, 9, 16, 25
  }
  return a;
}

TEST(size_and_bounds) {
  Array<int, 2> a({-1, 0}, {4, 3}); // (-1:2, 0:2)
  CHECK_EQ(fortran::size(a), 12);
  CHECK_EQ(fortran::size(a, 1), 4);
  CHECK_EQ(fortran::size(a, 2), 3);
  CHECK_EQ(fortran::lbound(a, 1), -1);
  CHECK_EQ(fortran::ubound(a, 1), 2);
  CHECK_EQ(fortran::lbound(a, 2), 0);
}

TEST(sum_and_product) {
  auto a = make_squares();
  CHECK_EQ(fortran::sum(a), 55);
  CHECK_EQ(fortran::product(a), 1 * 4 * 9 * 16 * 25);
}

TEST(maxval_minval) {
  auto a = make_squares();
  CHECK_EQ(fortran::maxval(a), 25);
  CHECK_EQ(fortran::minval(a), 1);
}

TEST(count_any_all) {
  Array<bool, 1> b({4});
  b(1) = true;
  b(2) = false;
  b(3) = true;
  b(4) = false;
  CHECK_EQ(fortran::count(b), 2);
  CHECK(fortran::any(b));
  CHECK(!fortran::all(b));
  b.fill(true);
  CHECK(fortran::all(b));
}

TEST(dot_product) {
  Array<int, 1> a({3});
  Array<int, 1> b({3});
  a(1) = 1; a(2) = 2; a(3) = 3;
  b(1) = 4; b(2) = 5; b(3) = 6;
  CHECK_EQ(fortran::dot_product(a, b), 4 + 10 + 18);
}

TEST(reductions_work_on_strided_ref) {
  // A non-contiguous view (every other element) must still reduce
  // correctly via for_each.
  int buf[6] = {1, 99, 2, 99, 3, 99};
  ArrayRef<int, 1> r(buf, {1}, {3}, {2}); // 1, 2, 3
  CHECK_EQ(fortran::sum(r), 6);
  CHECK_EQ(fortran::maxval(r), 3);
}

TEST(reductions_over_2d) {
  Array<int, 2> m({2, 3});
  int v = 1;
  for (int j = 1; j <= 3; ++j) {
    for (int i = 1; i <= 2; ++i) {
      m(i, j) = v++; // 1..6
    }
  }
  CHECK_EQ(fortran::sum(m), 21);
  CHECK_EQ(fortran::maxval(m), 6);
}

FORTRAN_RT_TEST_MAIN()

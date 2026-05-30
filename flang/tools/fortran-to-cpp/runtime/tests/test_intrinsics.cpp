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

TEST(matmul_matrix_matrix) {
  // a (2x3) * b (3x2) = c (2x2)
  Array<int, 2> a({2, 3});
  Array<int, 2> b({3, 2});
  int v = 1;
  for (int j = 1; j <= 3; ++j)
    for (int i = 1; i <= 2; ++i)
      a(i, j) = (i - 1) * 3 + j; // [[1,2,3],[4,5,6]]
  // b = transpose(a) = [[1,4],[2,5],[3,6]]
  for (int i = 1; i <= 2; ++i)
    for (int j = 1; j <= 3; ++j)
      b(j, i) = a(i, j);
  auto c = fortran::matmul(a, b);
  CHECK_EQ(c.extent(1), 2);
  CHECK_EQ(c.extent(2), 2);
  CHECK_EQ(c(1, 1), 1 + 4 + 9);
  CHECK_EQ(c(2, 2), 16 + 25 + 36);
}

TEST(matmul_matrix_vector) {
  Array<int, 2> a({2, 2});
  Array<int, 1> x({2});
  a(1, 1) = 1; a(1, 2) = 2; a(2, 1) = 3; a(2, 2) = 4;
  x(1) = 5; x(2) = 6;
  auto y = fortran::matmul(a, x); // [1*5+2*6, 3*5+4*6] = [17, 39]
  CHECK_EQ(y.size(), 2);
  CHECK_EQ(y(1), 17);
  CHECK_EQ(y(2), 39);
}

TEST(transpose_2d) {
  Array<int, 2> a({2, 3});
  int v = 1;
  for (int j = 1; j <= 3; ++j)
    for (int i = 1; i <= 2; ++i)
      a(i, j) = v++;
  auto t = fortran::transpose(a);
  CHECK_EQ(t.extent(1), 3);
  CHECK_EQ(t.extent(2), 2);
  CHECK_EQ(t(1, 2), a(2, 1));
  CHECK_EQ(t(3, 1), a(1, 3));
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

TEST(eoshift_rank1_default_boundary) {
  Array<int, 1> a({3});
  a(1) = 1;
  a(2) = 2;
  a(3) = 3;
  auto r = fortran::eoshift(a, 1); // [2, 3, 0]
  CHECK_EQ(r(1), 2);
  CHECK_EQ(r(2), 3);
  CHECK_EQ(r(3), 0);
}

TEST(eoshift_negative_shift_with_boundary) {
  Array<int, 1> a({3});
  a(1) = 1;
  a(2) = 2;
  a(3) = 3;
  auto r = fortran::eoshift(a, -1, 9); // [9, 1, 2]
  CHECK_EQ(r(1), 9);
  CHECK_EQ(r(2), 1);
  CHECK_EQ(r(3), 2);
}

TEST(spread_dim1_replicates_rows) {
  Array<int, 1> v({3});
  v(1) = 1;
  v(2) = 2;
  v(3) = 3;
  auto m = fortran::spread(v, 1, 2); // shape (2, 3), m(i, j) = v(j)
  CHECK_EQ(m.extent(1), 2);
  CHECK_EQ(m.extent(2), 3);
  CHECK_EQ(m(1, 2), 2);
  CHECK_EQ(m(2, 3), 3);
}

TEST(spread_dim2_replicates_cols) {
  Array<int, 1> v({3});
  v(1) = 1;
  v(2) = 2;
  v(3) = 3;
  auto m = fortran::spread(v, 2, 4); // shape (3, 4), m(i, j) = v(i)
  CHECK_EQ(m.extent(1), 3);
  CHECK_EQ(m.extent(2), 4);
  CHECK_EQ(m(2, 1), 2);
  CHECK_EQ(m(3, 4), 3);
}

TEST(bit_logical_ops) {
  CHECK_EQ(fortran::iand(12, 10), 8);
  CHECK_EQ(fortran::ior(12, 10), 14);
  CHECK_EQ(fortran::ieor(12, 10), 6);
}

TEST(ishft_left_and_right) {
  CHECK_EQ(fortran::ishft(1, 3), 8);
  CHECK_EQ(fortran::ishft(16, -2), 4);
  CHECK_EQ(fortran::ishft(1, 100), 0); // shift beyond width -> 0
}

TEST(btest_ibset_ibclr) {
  CHECK(fortran::btest(5, 0));   // 101b, bit 0 set
  CHECK(!fortran::btest(5, 1));  // bit 1 clear
  CHECK_EQ(fortran::ibset(0, 4), 16);
  CHECK_EQ(fortran::ibclr(15, 1), 13);
}

TEST(numeric_inquiry) {
  float f = 0.0f;
  double d = 0.0;
  std::int32_t i32 = 0;
  std::int64_t i64 = 0;
  CHECK(fortran::huge(f) > 1.0e30f);
  CHECK(fortran::tiny(f) > 0.0f);
  CHECK(fortran::epsilon(f) > 0.0f);
  CHECK_EQ(fortran::huge(i32), 2147483647);
  CHECK_EQ(fortran::kind(f), 4);
  CHECK_EQ(fortran::kind(d), 8);
  CHECK_EQ(fortran::kind(i64), 8);
  CHECK_EQ(fortran::bit_size(i32), 32);
  CHECK_EQ(fortran::bit_size(i64), 64);
}

FORTRAN_RT_TEST_MAIN()

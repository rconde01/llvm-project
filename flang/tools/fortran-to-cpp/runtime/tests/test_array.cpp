//===-- test_array.cpp - Unit tests for Array and ArrayRef ------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "fortran/array.hpp"
#include "fortran/array_ref.hpp"
#include "test_main.hpp"

#include <stdexcept>
#include <type_traits>

using fortran::Array;
using fortran::ArrayRef;
using fortran::Bounds;
using fortran::index_t;

// ---- Construction & bounds ------------------------------------------------

TEST(default_construction_is_empty) {
  Array<int, 1> a;
  CHECK(a.empty());
  CHECK_EQ(a.size(), 0);
}

TEST(rank1_default_lower_bound_is_one) {
  Array<int, 1> a({5});
  CHECK_EQ(a.lbound(1), 1);
  CHECK_EQ(a.ubound(1), 5);
  CHECK_EQ(a.extent(1), 5);
  CHECK_EQ(a.size(), 5);
}

TEST(rank1_explicit_lower_bound) {
  Array<double, 1> b({-5}, {11}); // -5:5
  CHECK_EQ(b.lbound(1), -5);
  CHECK_EQ(b.ubound(1), 5);
  CHECK_EQ(b.extent(1), 11);
  CHECK_EQ(b.size(), 11);
}

TEST(rank2_default_bounds) {
  Array<int, 2> a({3, 4});
  CHECK_EQ(a.lbound(1), 1);
  CHECK_EQ(a.ubound(1), 3);
  CHECK_EQ(a.lbound(2), 1);
  CHECK_EQ(a.ubound(2), 4);
  CHECK_EQ(a.size(), 12);
}

TEST(rank3_arbitrary_lower_bounds) {
  Array<int, 3> a({-1, 0, 5}, {4, 3, 2});
  CHECK_EQ(a.lbound(1), -1);
  CHECK_EQ(a.ubound(1), 2);
  CHECK_EQ(a.lbound(3), 5);
  CHECK_EQ(a.ubound(3), 6);
  CHECK_EQ(a.size(), 24);
}

// ---- Indexing -------------------------------------------------------------

TEST(rank1_indexing_roundtrip) {
  Array<int, 1> a({5});
  for (index_t i = 1; i <= 5; ++i) {
    a(i) = static_cast<int>(i * 10);
  }
  for (index_t i = 1; i <= 5; ++i) {
    CHECK_EQ(a(i), static_cast<int>(i * 10));
  }
}

TEST(rank2_indexing_is_column_major) {
  // For a(1,1), a(2,1), a(1,2), a(2,2) layout should be
  // contiguous in memory: a(1,1) at data[0], a(2,1) at data[1],
  // a(1,2) at data[2], a(2,2) at data[3].
  Array<int, 2> a({2, 2});
  a(1, 1) = 11;
  a(2, 1) = 21;
  a(1, 2) = 12;
  a(2, 2) = 22;
  CHECK_EQ(a.data()[0], 11);
  CHECK_EQ(a.data()[1], 21);
  CHECK_EQ(a.data()[2], 12);
  CHECK_EQ(a.data()[3], 22);
}

TEST(rank2_indexing_with_offset_lower_bounds) {
  Array<int, 2> a({-1, 0}, {3, 3}); // a(-1:1, 0:2)
  a(-1, 0) = 100;
  a(1, 2) = 999;
  CHECK_EQ(a(-1, 0), 100);
  CHECK_EQ(a(1, 2), 999);
  // The two corner stores should be at the very first and very last
  // positions of the column-major buffer.
  CHECK_EQ(a.data()[0], 100);
  CHECK_EQ(a.data()[8], 999);
}

TEST(rank3_indexing_roundtrip) {
  Array<int, 3> a({2, 3, 4});
  int counter = 0;
  for (index_t k = 1; k <= 4; ++k) {
    for (index_t j = 1; j <= 3; ++j) {
      for (index_t i = 1; i <= 2; ++i) {
        a(i, j, k) = counter++;
      }
    }
  }
  // Column-major: i is fastest, then j, then k.  The buffer should
  // therefore have been filled in linear order 0, 1, 2, ...
  for (index_t off = 0; off < a.size(); ++off) {
    CHECK_EQ(a.data()[off], static_cast<int>(off));
  }
}

// ---- Bounds checking ------------------------------------------------------
// These only fire in debug builds (FORTRAN_RT_BOUNDS_CHECK or !NDEBUG).
// In release, bounds checks compile to a no-op as documented in
// array.hpp.

#if defined(FORTRAN_RT_BOUNDS_CHECK) || !defined(NDEBUG)
TEST(bounds_check_lower) {
  Array<int, 1> a({5});
  CHECK_THROWS(a(0), std::out_of_range);
}

TEST(bounds_check_upper) {
  Array<int, 1> a({5});
  CHECK_THROWS(a(6), std::out_of_range);
}

TEST(bounds_check_in_higher_rank) {
  Array<int, 2> a({3, 3});
  CHECK_THROWS(a(4, 1), std::out_of_range);
  CHECK_THROWS(a(1, 0), std::out_of_range);
}
#endif

// ---- Move semantics & clone ----------------------------------------------

TEST(is_move_constructible_but_not_copy_constructible) {
  static_assert(std::is_move_constructible_v<Array<int, 1>>);
  static_assert(!std::is_copy_constructible_v<Array<int, 1>>);
  static_assert(std::is_move_assignable_v<Array<int, 1>>);
  static_assert(!std::is_copy_assignable_v<Array<int, 1>>);
}

TEST(move_transfers_ownership) {
  Array<int, 1> a({3});
  a(1) = 7;
  a(2) = 8;
  a(3) = 9;
  Array<int, 1> b = std::move(a);
  CHECK_EQ(b(1), 7);
  CHECK_EQ(b(2), 8);
  CHECK_EQ(b(3), 9);
  CHECK(a.empty()); // moved-from
}

TEST(clone_produces_independent_copy) {
  Array<int, 1> a({3});
  a(1) = 10;
  a(2) = 20;
  a(3) = 30;
  Array<int, 1> b = a.clone();
  CHECK_EQ(b(1), 10);
  CHECK_EQ(b(2), 20);
  CHECK_EQ(b(3), 30);
  a(2) = 999;
  CHECK_EQ(b(2), 20); // independent
}

// ---- fill -----------------------------------------------------------------

TEST(fill_sets_every_element) {
  Array<double, 2> a({4, 5});
  a.fill(3.14);
  for (index_t j = 1; j <= 5; ++j) {
    for (index_t i = 1; i <= 4; ++i) {
      CHECK_EQ(a(i, j), 3.14);
    }
  }
}

// ---- ArrayRef -------------------------------------------------------------

TEST(arrayref_view_of_owning_array) {
  Array<int, 2> a({3, 3});
  a(2, 2) = 42;
  ArrayRef<int, 2> r = a;
  CHECK_EQ(r(2, 2), 42);
  r(2, 2) = 7;
  CHECK_EQ(a(2, 2), 7); // same storage
}

TEST(arrayref_const_view_of_const_array) {
  Array<int, 1> a({3});
  a(1) = 11;
  a(2) = 22;
  a(3) = 33;
  const auto &ca = a;
  ArrayRef<const int, 1> r = ca;
  CHECK_EQ(r(1), 11);
  CHECK_EQ(r(3), 33);
}

TEST(arrayref_from_raw_buffer) {
  int buf[6] = {1, 2, 3, 4, 5, 6};
  ArrayRef<int, 2> r(buf, {{2, 3}});
  // column-major: r(1,1)=1, r(2,1)=2, r(1,2)=3, r(2,2)=4, r(1,3)=5, r(2,3)=6
  CHECK_EQ(r(1, 1), 1);
  CHECK_EQ(r(2, 1), 2);
  CHECK_EQ(r(1, 3), 5);
  CHECK_EQ(r(2, 3), 6);
}

TEST(arrayref_with_offset_lower_bounds) {
  int buf[5] = {10, 20, 30, 40, 50};
  // 1D view with lbound = -2 means indices -2..2 map to buf[0..4].
  ArrayRef<int, 1> r(buf, /*lower=*/{-2}, /*extents=*/{5});
  CHECK_EQ(r(-2), 10);
  CHECK_EQ(r(2), 50);
  CHECK_EQ(r.lbound(1), -2);
  CHECK_EQ(r.ubound(1), 2);
}

TEST(arrayref_is_contiguous_for_default_strides) {
  int buf[6] = {0};
  ArrayRef<int, 2> r(buf, {{2, 3}});
  CHECK(r.is_contiguous());
}

TEST(arrayref_with_non_default_strides_is_not_contiguous) {
  // A view that walks every other element of a 1D buffer.  This is the
  // pattern produced by a slice like a(1:9:2) in Fortran.
  int buf[9] = {0, 1, 2, 3, 4, 5, 6, 7, 8};
  ArrayRef<int, 1> r(buf, {1}, {5}, {2}); // 5 elements, stride 2
  CHECK_EQ(r(1), 0);
  CHECK_EQ(r(2), 2);
  CHECK_EQ(r(3), 4);
  CHECK_EQ(r(4), 6);
  CHECK_EQ(r(5), 8);
  CHECK(!r.is_contiguous());
}

// ---- Allocatable lifecycle ------------------------------------------------

TEST(default_array_is_not_allocated) {
  Array<int, 1> a;
  CHECK(!a.allocated());
  CHECK(a.empty());
}

TEST(reallocate_via_move_assign) {
  Array<int, 1> a;             // unallocated (real, allocatable :: a(:))
  a = Array<int, 1>({4});      // allocate(a(4))
  CHECK(a.allocated());
  CHECK_EQ(a.size(), 4);
  a(1) = 7;
  CHECK_EQ(a(1), 7);
}

TEST(deallocate_releases_storage) {
  Array<int, 1> a({4});
  CHECK(a.allocated());
  a.deallocate();
  CHECK(!a.allocated());
  CHECK_EQ(a.size(), 0);
}

// ---- Sections -------------------------------------------------------------

TEST(rank1_contiguous_section) {
  Array<int, 1> a({10});
  for (index_t i = 1; i <= 10; ++i) {
    a(i) = static_cast<int>(i);
  }
  auto s = a.section(3, 7); // a(3:7) -> 5 elements, 1-based
  CHECK_EQ(s.size(), 5);
  CHECK_EQ(s(1), 3);
  CHECK_EQ(s(5), 7);
}

TEST(rank1_strided_section) {
  Array<int, 1> a({10});
  for (index_t i = 1; i <= 10; ++i) {
    a(i) = static_cast<int>(i);
  }
  auto s = a.section(2, 10, 2); // a(2:10:2) -> 2,4,6,8,10
  CHECK_EQ(s.size(), 5);
  CHECK_EQ(s(1), 2);
  CHECK_EQ(s(5), 10);
  CHECK(!s.is_contiguous());
}

TEST(section_writes_through_to_parent) {
  Array<int, 1> a({6});
  a.fill(0);
  auto s = a.section(2, 4); // a(2:4)
  s(1) = 20;
  s(3) = 40;
  CHECK_EQ(a(2), 20);
  CHECK_EQ(a(4), 40);
  CHECK_EQ(a(1), 0);
}

// ---- Bounds type helper ---------------------------------------------------

TEST(bounds_extent_is_inclusive) {
  Bounds b{2, 7};
  CHECK_EQ(b.extent(), 6);
  Bounds c{-3, 3};
  CHECK_EQ(c.extent(), 7);
}

FORTRAN_RT_TEST_MAIN()

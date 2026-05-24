//===-- test_equiv.cpp - Tests for EquivSlot and bit_cast -------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "fortran/array_ref.hpp"
#include "fortran/equiv.hpp"
#include "test_main.hpp"

#include <array>
#include <cstdint>

using fortran::ArrayRef;
using fortran::EquivSlot;

// Case 1 — same-size type pun: REAL <-> INTEGER over one 4-byte cell.
struct XBits {
  std::array<std::byte, sizeof(float)> _store{};
  EquivSlot<float, 0>        x   {_store.data()};
  EquivSlot<std::int32_t, 0> bits{_store.data()};
};

TEST(equiv_type_pun_float_to_int_and_back) {
  XBits e;
  e.x = 1.0f;
  // IEEE-754: 1.0f bit pattern is 0x3F800000.
  CHECK_EQ(static_cast<std::int32_t>(e.bits), 0x3F800000);
  e.bits = 0;
  CHECK_EQ(static_cast<float>(e.x), 0.0f);
}

TEST(equiv_slot_arithmetic_helpers) {
  struct One {
    std::array<std::byte, sizeof(std::int32_t)> _store{};
    EquivSlot<std::int32_t, 0> n{_store.data()};
  };
  One e;
  e.n = 5;
  e.n += 3;
  CHECK_EQ(static_cast<std::int32_t>(e.n), 8);
  e.n -= 10;
  CHECK_EQ(static_cast<std::int32_t>(e.n), -2);
}

// Case 2 — array overlap with offset.  100 floats; ``tail`` views the
// last 10 starting at offset 90.  This is the EQUIVALENCE pattern:
//
//   real :: big(100), tail(10)
//   equivalence (big(91), tail(1))
struct BigTail {
  std::array<std::byte, 100 * sizeof(float)> _store{};
  ArrayRef<float, 1> big {
      reinterpret_cast<float *>(_store.data()), {{1}}, {{100}}};
  ArrayRef<float, 1> tail{
      reinterpret_cast<float *>(_store.data()) + 90, {{1}}, {{10}}};
};

TEST(equiv_array_overlap_writes_through) {
  BigTail e;
  for (int i = 1; i <= 100; ++i) {
    e.big(i) = static_cast<float>(i);
  }
  // tail(1) should map to big(91), tail(10) to big(100).
  CHECK_EQ(e.tail(1), 91.0f);
  CHECK_EQ(e.tail(10), 100.0f);

  // Writes through tail should be visible via big.
  e.tail(5) = -1.0f;
  CHECK_EQ(e.big(95), -1.0f);
}

// ---- fortran::bit_cast helper ---------------------------------------------

TEST(bit_cast_round_trip) {
  const float f = -3.14f;
  const auto bits = fortran::bit_cast<std::uint32_t>(f);
  CHECK_EQ(fortran::bit_cast<float>(bits), f);
}

FORTRAN_RT_TEST_MAIN()

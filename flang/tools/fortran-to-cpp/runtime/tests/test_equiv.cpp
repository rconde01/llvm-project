//===-- test_equiv.cpp - Tests for EquivSlot and bit_cast -------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "fortran/array_ref.hpp"
#include "fortran/equiv.hpp"
#include "fortran/io.hpp"
#include "test_main.hpp"

#include <array>
#include <cstdint>
#include <vector>

using ftn::ArrayRef;
using ftn::EquivArray;
using ftn::EquivSlot;

// Case 1 — same-size type pun: REAL <-> INTEGER over one 4-byte cell.
struct XBits {
  std::array<std::byte, sizeof(float)> _store{};
  EquivSlot<float, 0>        x   {_store.data()};
  EquivSlot<int32_t, 0> bits{_store.data()};
};

TEST(equiv_type_pun_float_to_int_and_back) {
  XBits e;
  e.x = 1.0f;
  // IEEE-754: 1.0f bit pattern is 0x3F800000.
  CHECK_EQ(static_cast<int32_t>(e.bits), 0x3F800000);
  e.bits = 0;
  CHECK_EQ(static_cast<float>(e.x), 0.0f);
}

TEST(equiv_slot_arithmetic_helpers) {
  struct One {
    std::array<std::byte, sizeof(int32_t)> _store{};
    EquivSlot<int32_t, 0> n{_store.data()};
  };
  One e;
  e.n = 5;
  e.n += 3;
  CHECK_EQ(static_cast<int32_t>(e.n), 8);
  e.n -= 10;
  CHECK_EQ(static_cast<int32_t>(e.n), -2);
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

// ---- EquivArray integration: ArrayRef view, byte I/O, elem_tail ----------
//
// The SPICE DAF buffers EQUIVALENCE a DOUBLE array onto an INTEGER one and
// then (a) pass the whole buffer to an array dummy, (b) read it element by
// element through unformatted I/O, and (c) sequence-associate an element.

// A 2-double buffer aliased to 4 int32s (the DPBUF/INBUF pun).
struct DafBuf {
  alignas(8) std::array<std::byte, 2 * sizeof(double)> _store{};
  EquivArray<double, 2, 0>       dp{_store.data()};
  EquivArray<int32_t, 4, 0> in{_store.data()};
};

TEST(equivarray_view_as_arrayref_writes_through) {
  DafBuf e;
  // A callee taking an ArrayRef writes doubles directly into the buffer.
  auto fill = [](ArrayRef<double, 1> a) { a(1) = 1.5; a(2) = 2.5; };
  fill(e.dp);  // implicit EquivArray -> ArrayRef
  CHECK_EQ(static_cast<double>(e.dp(1)), 1.5);
  // Low 32 bits of 1.5 are 0; high 32 are 0x3FF80000.
  CHECK_EQ(static_cast<int32_t>(e.in(1)), 0);
  CHECK_EQ(static_cast<int32_t>(e.in(2)), 0x3FF80000);
}

TEST(equivarray_cell_byte_io_round_trip) {
  DafBuf e;
  e.dp(1) = 1.5;
  e.dp(2) = 2.5;
  // Serialize the buffer element-by-element (unformatted I/O path).
  std::vector<std::byte> rec;
  ftn::io::append_bytes(rec, e.dp(1));
  ftn::io::append_bytes(rec, e.dp(2));
  // Clear, then read it back through the element proxies.
  e.dp(1) = 0.0;
  e.dp(2) = 0.0;
  std::size_t off = 0;
  off = ftn::io::take_bytes(rec, off, e.dp(1));
  off = ftn::io::take_bytes(rec, off, e.dp(2));
  CHECK_EQ(static_cast<double>(e.dp(1)), 1.5);
  CHECK_EQ(static_cast<double>(e.dp(2)), 2.5);
  CHECK_EQ(off, rec.size());
}

TEST(equivarray_elem_tail_views_from_element) {
  alignas(8) std::array<std::byte, 3 * sizeof(double)> store{};
  EquivArray<double, 3, 0> d{store.data()};
  d(1) = 9.5;
  // A dummy that views the buffer from element 2 onward.
  auto fill2 = [](ArrayRef<double, 1> a) { a(1) = 1.5; a(2) = 2.5; };
  fill2(ftn::elem_tail(d, 2));
  CHECK_EQ(static_cast<double>(d(1)), 9.5);  // untouched
  CHECK_EQ(static_cast<double>(d(2)), 1.5);
  CHECK_EQ(static_cast<double>(d(3)), 2.5);
}

// ---- ftn::bit_cast helper ---------------------------------------------

TEST(bit_cast_round_trip) {
  const float f = -3.14f;
  const auto bits = ftn::bit_cast<uint32_t>(f);
  CHECK_EQ(ftn::bit_cast<float>(bits), f);
}

FORTRAN_RT_TEST_MAIN()

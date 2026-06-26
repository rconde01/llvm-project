//===-- test_time.cpp - Unit tests for fortran/time.hpp ---------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "fortran/time.hpp"

#include "test_main.hpp"

#include <cstdint>
#include <limits>
#include <thread>

TEST(cpu_time_is_nonnegative_and_monotonic) {
  float a = -1.0f;
  ftn::cpu_time(a);
  CHECK(a >= 0.0f);
  // Burn a little CPU so the second reading can't be earlier.
  volatile double acc = 0.0;
  for (int i = 0; i < 1000000; ++i) {
    acc += i;
  }
  float b = -1.0f;
  ftn::cpu_time(b);
  CHECK(b >= a);
}

TEST(system_clock_count_only) {
  int64_t c = -1;
  ftn::system_clock(c);
  CHECK(c >= 0);
}

TEST(system_clock_rate_is_1000) {
  int32_t c = 0;
  int32_t rate = 0;
  ftn::system_clock(c, rate);
  CHECK_EQ(rate, 1000);
}

TEST(system_clock_count_max_matches_type) {
  int32_t c = 0;
  int32_t rate = 0;
  int32_t cmax = 0;
  ftn::system_clock(c, rate, cmax);
  CHECK_EQ(cmax, std::numeric_limits<int32_t>::max());
}

TEST(system_clock_is_monotonic) {
  int64_t c1 = 0;
  ftn::system_clock(c1);
  std::this_thread::sleep_for(std::chrono::milliseconds(2));
  int64_t c2 = 0;
  ftn::system_clock(c2);
  CHECK(c2 >= c1);
}

FORTRAN_RT_TEST_MAIN()

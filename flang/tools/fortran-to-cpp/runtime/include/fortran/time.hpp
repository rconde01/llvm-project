//===-- fortran/time.hpp - Timing intrinsic subroutines ---------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Implementations of Fortran's timing intrinsic *subroutines* —
// ``CPU_TIME`` and ``SYSTEM_CLOCK`` — in the ``ftn::`` namespace.
// These are pure system queries (no shared mutable state), so they are
// safe to call from multiple threads.
//
// SYSTEM_CLOCK reports a millisecond tick count with a fixed
// ``count_rate`` of 1000.  The Fortran standard makes the rate
// processor-dependent; milliseconds keep a default 32-bit ``count`` from
// wrapping for ~24 days while staying precise enough for typical elapsed
// timing (``(c2 - c1) / real(rate)``).  Use ``integer(8)`` arguments for
// long-running or finer measurements.
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_RT_TIME_HPP
#define FORTRAN_RT_TIME_HPP

#include <chrono>
#include <cstdio>
#include <ctime>
#include <limits>
#include <string_view>

namespace ftn {

/// CPU_TIME(time): processor CPU time consumed so far, in seconds.
template <typename T> void cpu_time(T &seconds) noexcept {
  seconds = static_cast<T>(std::clock()) / static_cast<T>(CLOCKS_PER_SEC);
}

namespace detail {
inline long long steady_millis() noexcept {
  const auto now = std::chrono::steady_clock::now().time_since_epoch();
  return std::chrono::duration_cast<std::chrono::milliseconds>(now).count();
}
} // namespace detail

/// SYSTEM_CLOCK(count): a monotonic millisecond tick count.
template <typename C> void system_clock(C &count) noexcept {
  count = static_cast<C>(detail::steady_millis());
}

/// SYSTEM_CLOCK(count, count_rate): rate is fixed at 1000 (ms ticks).
template <typename C, typename R>
void system_clock(C &count, R &count_rate) noexcept {
  system_clock(count);
  count_rate = static_cast<R>(1000);
}

/// SYSTEM_CLOCK(count, count_rate, count_max): count_max is the largest
/// value ``count`` can hold before it wraps.
template <typename C, typename R, typename M>
void system_clock(C &count, R &count_rate, M &count_max) noexcept {
  system_clock(count, count_rate);
  count_max = std::numeric_limits<C>::max();
}

/// DATE_AND_TIME(date, time, zone, values): the wall-clock date and time.
/// ``date`` / ``time`` / ``zone`` are character views (filled
/// "CCYYMMDD" / "hhmmss.sss" / "+-hhmm"); ``values`` is an 8-element
/// integer array filled per the standard: year, month, day, UTC-offset
/// minutes, hour, minute, second, millisecond.  (Only the 4-argument form
/// the corpus uses is provided.)
template <typename D, typename T, typename Z, typename V>
void date_and_time(D &&date, T &&time, Z &&zone, V &&values) noexcept {
  const auto now = std::chrono::system_clock::now();
  const std::time_t tt = std::chrono::system_clock::to_time_t(now);
  std::tm lt{};
  localtime_r(&tt, &lt);
  const long long ms =
      std::chrono::duration_cast<std::chrono::milliseconds>(
          now.time_since_epoch())
          .count() %
      1000;
  const long off_min = lt.tm_gmtoff / 60;
  char buf[48];
  std::snprintf(buf, sizeof buf, "%04d%02d%02d", lt.tm_year + 1900,
                lt.tm_mon + 1, lt.tm_mday);
  date = std::string_view{buf};
  std::snprintf(buf, sizeof buf, "%02d%02d%02d.%03lld", lt.tm_hour, lt.tm_min,
                lt.tm_sec, ms);
  time = std::string_view{buf};
  std::snprintf(buf, sizeof buf, "%+03ld%02ld", off_min / 60,
                (off_min < 0 ? -off_min : off_min) % 60);
  zone = std::string_view{buf};
  values(1) = static_cast<int>(lt.tm_year + 1900);
  values(2) = static_cast<int>(lt.tm_mon + 1);
  values(3) = static_cast<int>(lt.tm_mday);
  values(4) = static_cast<int>(off_min);
  values(5) = static_cast<int>(lt.tm_hour);
  values(6) = static_cast<int>(lt.tm_min);
  values(7) = static_cast<int>(lt.tm_sec);
  values(8) = static_cast<int>(ms);
}

} // namespace ftn

#endif // FORTRAN_RT_TIME_HPP

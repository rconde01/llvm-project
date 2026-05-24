//===-- fortran/time.hpp - Timing intrinsic subroutines ---------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Implementations of Fortran's timing intrinsic *subroutines* —
// ``CPU_TIME`` and ``SYSTEM_CLOCK`` — in the ``fortran::`` namespace.
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
#include <ctime>
#include <limits>

namespace fortran {

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

} // namespace fortran

#endif // FORTRAN_RT_TIME_HPP

//===-- fortran/intrinsics.hpp - Array intrinsic functions ------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Free-function implementations of Fortran's array intrinsics, in the
// ``fortran::`` namespace.  The converter maps the Fortran intrinsic
// names (SIZE, SUM, MAXVAL, ...) onto these.  Each is templated on the
// array-like argument so it accepts both owning ``Array`` and
// non-owning ``ArrayRef`` of any element constness.
//
// Inquiry intrinsics (SIZE / LBOUND / UBOUND) just forward to the
// array's own methods.  Reductions (SUM / PRODUCT / MAXVAL / MINVAL /
// COUNT / ANY / ALL / DOT_PRODUCT) use the array's ``for_each`` visitor
// so they work regardless of storage strides.
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_RT_INTRINSICS_HPP
#define FORTRAN_RT_INTRINSICS_HPP

#include "array.hpp"

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>
#include <string_view>

namespace fortran {

// ---- Numeric conversion helpers (rounding / truncating forms) -------------
// The plain casts INT/REAL/DBLE are emitted as static_cast directly; these
// are the ones that round or truncate.

/// NINT — round to the nearest integer.
template <typename T> std::int32_t nint(T x) {
  return static_cast<std::int32_t>(std::llround(x));
}

/// AINT — truncate toward zero, result stays real.
template <typename T> T aint(T x) { return std::trunc(x); }

/// ANINT — round to nearest, result stays real.
template <typename T> T anint(T x) { return std::round(x); }

// ---- Character intrinsics -------------------------------------------------
// These accept anything convertible to std::string_view, so they work on
// FortranString<N> (implicit conversion), string_view literals, and
// std::string alike.

inline index_t len(std::string_view s) noexcept {
  return static_cast<index_t>(s.size());
}

inline index_t len_trim(std::string_view s) noexcept {
  std::size_t e = s.size();
  while (e > 0 && s[e - 1] == ' ') {
    --e;
  }
  return static_cast<index_t>(e);
}

/// TRIM — the string without trailing blanks (a non-owning view).
inline std::string_view trim(std::string_view s) noexcept {
  return s.substr(0, static_cast<std::size_t>(len_trim(s)));
}

/// INDEX(string, substring) — 1-based position of the first occurrence,
/// or 0 if not present (matching Fortran).
inline index_t index(std::string_view s, std::string_view sub) noexcept {
  const auto pos = s.find(sub);
  return pos == std::string_view::npos ? 0 : static_cast<index_t>(pos) + 1;
}

/// ADJUSTL — move leading blanks to the end (length preserved).
inline std::string adjustl(std::string_view s) {
  std::size_t i = 0;
  while (i < s.size() && s[i] == ' ') {
    ++i;
  }
  std::string r(s.substr(i));
  r.append(i, ' ');
  return r;
}

/// ADJUSTR — move trailing blanks to the front (length preserved).
inline std::string adjustr(std::string_view s) {
  std::size_t e = s.size();
  while (e > 0 && s[e - 1] == ' ') {
    --e;
  }
  std::string r(s.size() - e, ' ');
  r.append(s.substr(0, e));
  return r;
}

/// Character concatenation (Fortran ``//``).  Returns a fresh string so
/// it composes with any mix of FortranString / string_view / literal
/// operands.
inline std::string concat(std::string_view a, std::string_view b) {
  std::string r;
  r.reserve(a.size() + b.size());
  r.append(a);
  r.append(b);
  return r;
}

// ---- Inquiry intrinsics ---------------------------------------------------

template <typename A> index_t size(const A &a) noexcept { return a.size(); }

template <typename A> index_t size(const A &a, int dim) noexcept {
  return a.extent(static_cast<std::size_t>(dim));
}

template <typename A> index_t lbound(const A &a, int dim) noexcept {
  return a.lbound(static_cast<std::size_t>(dim));
}

template <typename A> index_t ubound(const A &a, int dim) noexcept {
  return a.ubound(static_cast<std::size_t>(dim));
}

// ---- Reductions -----------------------------------------------------------

template <typename A> auto sum(const A &a) {
  typename A::value_type acc{};
  a.for_each([&](const auto &v) { acc += v; });
  return acc;
}

template <typename A> auto product(const A &a) {
  typename A::value_type acc{1};
  a.for_each([&](const auto &v) { acc *= v; });
  return acc;
}

template <typename A> auto maxval(const A &a) {
  using T = typename A::value_type;
  T best = std::numeric_limits<T>::lowest();
  a.for_each([&](const auto &v) {
    if (v > best) {
      best = v;
    }
  });
  return best;
}

template <typename A> auto minval(const A &a) {
  using T = typename A::value_type;
  T best = std::numeric_limits<T>::max();
  a.for_each([&](const auto &v) {
    if (v < best) {
      best = v;
    }
  });
  return best;
}

/// COUNT of true elements in a logical array.
template <typename A> index_t count(const A &a) {
  index_t n = 0;
  a.for_each([&](const auto &v) {
    if (v) {
      ++n;
    }
  });
  return n;
}

/// ANY — true if any element is true.
template <typename A> bool any(const A &a) {
  bool result = false;
  a.for_each([&](const auto &v) {
    if (v) {
      result = true;
    }
  });
  return result;
}

/// ALL — true if every element is true.
template <typename A> bool all(const A &a) {
  bool result = true;
  a.for_each([&](const auto &v) {
    if (!v) {
      result = false;
    }
  });
  return result;
}

// ---- MATMUL / TRANSPOSE (array-returning) ---------------------------------

/// MATMUL — matrix*matrix, matrix*vector, or vector*matrix.  Returns a
/// freshly-allocated, 1-based Array of the appropriate rank.
template <typename A, typename B> auto matmul(const A &a, const B &b) {
  using T = typename A::value_type;
  if constexpr (A::rank == 2 && B::rank == 2) {
    const index_t m = a.extent(1), kd = a.extent(2), n = b.extent(2);
    Array<T, 2> r({m, n});
    for (index_t j = 1; j <= n; ++j) {
      for (index_t i = 1; i <= m; ++i) {
        T s{};
        for (index_t k = 1; k <= kd; ++k) {
          s += a(i, k) * b(k, j);
        }
        r(i, j) = s;
      }
    }
    return r;
  } else if constexpr (A::rank == 2 && B::rank == 1) {
    const index_t m = a.extent(1), kd = a.extent(2);
    Array<T, 1> r({m});
    for (index_t i = 1; i <= m; ++i) {
      T s{};
      for (index_t k = 1; k <= kd; ++k) {
        s += a(i, k) * b(k);
      }
      r(i) = s;
    }
    return r;
  } else { // rank-1 * rank-2
    const index_t kd = a.extent(1), n = b.extent(2);
    Array<T, 1> r({n});
    for (index_t j = 1; j <= n; ++j) {
      T s{};
      for (index_t k = 1; k <= kd; ++k) {
        s += a(k) * b(k, j);
      }
      r(j) = s;
    }
    return r;
  }
}

/// TRANSPOSE of a rank-2 array.
template <typename A>
Array<typename A::value_type, 2> transpose(const A &a) {
  using T = typename A::value_type;
  const index_t m = a.extent(1), n = a.extent(2);
  Array<T, 2> r({n, m});
  for (index_t j = 1; j <= n; ++j) {
    for (index_t i = 1; i <= m; ++i) {
      r(j, i) = a(i, j);
    }
  }
  return r;
}

/// DOT_PRODUCT of two rank-1 arrays.  Iterates both in lockstep by
/// flat element order (valid for the column-major contiguous case the
/// translator produces).
template <typename A, typename B> auto dot_product(const A &a, const B &b) {
  typename A::value_type acc{};
  const index_t n = a.size();
  for (index_t i = 0; i < n; ++i) {
    acc += a.data()[i] * b.data()[i];
  }
  return acc;
}

} // namespace fortran

#endif // FORTRAN_RT_INTRINSICS_HPP

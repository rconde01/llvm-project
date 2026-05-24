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

#include <cstddef>
#include <limits>

namespace fortran {

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

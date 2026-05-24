//===-- fortran/array_ops.hpp - Elementwise array operators -----*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Eager elementwise operators on fortran::Array, producing a new Array.
//
// The converter expands whole-array *assignments* (a = b + c) into
// explicit loops with no temporaries.  But array-valued expressions
// used as *values* — e.g. a mask ``a > 2`` passed to PACK, or
// ``foo(b + c)`` — need an actual array result.  These operators
// provide that, at the cost of a temporary (acceptable for the
// value-context case, which is far less common than assignment).
//
// Provided for Array (x) Array, Array (x) scalar, and scalar (x) Array:
//   arithmetic  + - * /      -> Array<T, R>
//   comparison  < > <= >= == /= -> Array<bool, R>
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_RT_ARRAY_OPS_HPP
#define FORTRAN_RT_ARRAY_OPS_HPP

#include "array.hpp"

namespace fortran {

/// A fresh array with the same bounds/extents as ``a`` but element
/// type ``Res``.
template <typename Res, typename T, std::size_t R>
Array<Res, R> like(const Array<T, R> &a) {
  return Array<Res, R>(a.lower_bounds(), a.extents());
}

#define FORTRAN_RT_ELEMENTWISE_ARITH(OP)                                       \
  template <typename T, std::size_t R>                                         \
  Array<T, R> operator OP(const Array<T, R> &a, const Array<T, R> &b) {         \
    auto r = like<T>(a);                                                       \
    for (index_t i = 0; i < a.size(); ++i)                                     \
      r.data()[i] = a.data()[i] OP b.data()[i];                                \
    return r;                                                                  \
  }                                                                            \
  template <typename T, std::size_t R>                                         \
  Array<T, R> operator OP(const Array<T, R> &a, const T &s) {                   \
    auto r = like<T>(a);                                                       \
    for (index_t i = 0; i < a.size(); ++i)                                     \
      r.data()[i] = a.data()[i] OP s;                                          \
    return r;                                                                  \
  }                                                                            \
  template <typename T, std::size_t R>                                         \
  Array<T, R> operator OP(const T &s, const Array<T, R> &a) {                   \
    auto r = like<T>(a);                                                       \
    for (index_t i = 0; i < a.size(); ++i)                                     \
      r.data()[i] = s OP a.data()[i];                                          \
    return r;                                                                  \
  }

#define FORTRAN_RT_ELEMENTWISE_CMP(OP)                                         \
  template <typename T, std::size_t R>                                         \
  Array<bool, R> operator OP(const Array<T, R> &a, const Array<T, R> &b) {      \
    auto r = like<bool>(a);                                                    \
    for (index_t i = 0; i < a.size(); ++i)                                     \
      r.data()[i] = a.data()[i] OP b.data()[i];                                \
    return r;                                                                  \
  }                                                                            \
  template <typename T, std::size_t R>                                         \
  Array<bool, R> operator OP(const Array<T, R> &a, const T &s) {                \
    auto r = like<bool>(a);                                                    \
    for (index_t i = 0; i < a.size(); ++i)                                     \
      r.data()[i] = a.data()[i] OP s;                                          \
    return r;                                                                  \
  }                                                                            \
  template <typename T, std::size_t R>                                         \
  Array<bool, R> operator OP(const T &s, const Array<T, R> &a) {                \
    auto r = like<bool>(a);                                                    \
    for (index_t i = 0; i < a.size(); ++i)                                     \
      r.data()[i] = s OP a.data()[i];                                          \
    return r;                                                                  \
  }

FORTRAN_RT_ELEMENTWISE_ARITH(+)
FORTRAN_RT_ELEMENTWISE_ARITH(-)
FORTRAN_RT_ELEMENTWISE_ARITH(*)
FORTRAN_RT_ELEMENTWISE_ARITH(/)

FORTRAN_RT_ELEMENTWISE_CMP(<)
FORTRAN_RT_ELEMENTWISE_CMP(>)
FORTRAN_RT_ELEMENTWISE_CMP(<=)
FORTRAN_RT_ELEMENTWISE_CMP(>=)
FORTRAN_RT_ELEMENTWISE_CMP(==)
FORTRAN_RT_ELEMENTWISE_CMP(!=)

#undef FORTRAN_RT_ELEMENTWISE_ARITH
#undef FORTRAN_RT_ELEMENTWISE_CMP

} // namespace fortran

#endif // FORTRAN_RT_ARRAY_OPS_HPP

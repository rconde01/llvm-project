//===-- fortran/array_ops.hpp - Elementwise array operators -----*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Eager elementwise operators on array-valued expressions, producing a
// new owning Array.
//
// The converter expands whole-array *assignments* (``a = b + c`` with a
// plain Name target) into explicit loops with no temporaries.  But an
// array-valued expression used as a *value* — a mask ``a > 2`` passed to
// PACK, ``foo(b + c)``, or a module-parameter initializer
// ``w = (nodes(4:n) - nodes(0:m)) / 4`` — needs an actual array result.
// These operators provide that, at the cost of one temporary (acceptable
// for the value-context case, which is far less common than assignment).
// This mirrors the array-returning intrinsics (MATMUL, TRANSPOSE, ...).
//
// Operands may be ``Array`` or ``ArrayRef`` (so sections work), in any
// combination, and the two element types may differ (the result uses
// their ``std::common_type``).  Provided for array (x) array, array (x)
// scalar, and scalar (x) array:
//   arithmetic  + - * /          -> Array<common_type, R>
//   comparison  < > <= >= == !=  -> Array<bool, R>
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_RT_ARRAY_OPS_HPP
#define FORTRAN_RT_ARRAY_OPS_HPP

#include "array.hpp"
#include "array_ref.hpp"

#include <type_traits>

namespace fortran {
namespace detail {

/// An ``Array`` or ``ArrayRef`` — anything with an element type, a static
/// rank, conforming bounds, and a column-major linear element accessor.
template <typename A>
concept ArrayLike = requires(const std::remove_cvref_t<A> &a, index_t k) {
  typename std::remove_cvref_t<A>::value_type;
  { std::remove_cvref_t<A>::rank } -> std::convertible_to<std::size_t>;
  { a.size() } -> std::convertible_to<index_t>;
  a.lower_bounds();
  a.extents();
  a.linear_at(k);
};

template <typename A>
using elem_t = typename std::remove_cvref_t<A>::value_type;

template <typename A>
inline constexpr std::size_t rank_of = std::remove_cvref_t<A>::rank;

} // namespace detail

#define FORTRAN_RT_ELEMENTWISE(OP, RESULT)                                     \
  template <detail::ArrayLike A, detail::ArrayLike B>                          \
  auto operator OP(const A &a, const B &b) {                                   \
    using E = std::common_type_t<detail::elem_t<A>, detail::elem_t<B>>;        \
    Array<RESULT, detail::rank_of<A>> out(a.lower_bounds(), a.extents());      \
    for (index_t i = 0; i < a.size(); ++i)                                     \
      out.linear_at(i) =                                                       \
          static_cast<E>(a.linear_at(i)) OP static_cast<E>(b.linear_at(i));    \
    return out;                                                                \
  }                                                                            \
  template <detail::ArrayLike A, typename S>                                   \
    requires std::is_arithmetic_v<S>                                           \
  auto operator OP(const A &a, const S &s) {                                   \
    using E = std::common_type_t<detail::elem_t<A>, S>;                        \
    Array<RESULT, detail::rank_of<A>> out(a.lower_bounds(), a.extents());      \
    for (index_t i = 0; i < a.size(); ++i)                                     \
      out.linear_at(i) = static_cast<E>(a.linear_at(i)) OP static_cast<E>(s);  \
    return out;                                                                \
  }                                                                            \
  template <detail::ArrayLike A, typename S>                                   \
    requires std::is_arithmetic_v<S>                                           \
  auto operator OP(const S &s, const A &a) {                                   \
    using E = std::common_type_t<S, detail::elem_t<A>>;                        \
    Array<RESULT, detail::rank_of<A>> out(a.lower_bounds(), a.extents());      \
    for (index_t i = 0; i < a.size(); ++i)                                     \
      out.linear_at(i) = static_cast<E>(s) OP static_cast<E>(a.linear_at(i));  \
    return out;                                                                \
  }

FORTRAN_RT_ELEMENTWISE(+, E)
FORTRAN_RT_ELEMENTWISE(-, E)
FORTRAN_RT_ELEMENTWISE(*, E)
FORTRAN_RT_ELEMENTWISE(/, E)

FORTRAN_RT_ELEMENTWISE(<, bool)
FORTRAN_RT_ELEMENTWISE(>, bool)
FORTRAN_RT_ELEMENTWISE(<=, bool)
FORTRAN_RT_ELEMENTWISE(>=, bool)
FORTRAN_RT_ELEMENTWISE(==, bool)
FORTRAN_RT_ELEMENTWISE(!=, bool)

#undef FORTRAN_RT_ELEMENTWISE

} // namespace fortran

#endif // FORTRAN_RT_ARRAY_OPS_HPP

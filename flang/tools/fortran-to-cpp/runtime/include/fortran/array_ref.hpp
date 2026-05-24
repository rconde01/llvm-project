//===-- fortran/array_ref.hpp - Non-owning array view -----------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// fortran::ArrayRef<T, Rank>
//
// A non-owning view of an N-dimensional Fortran-style array.  Stores a
// base pointer, per-dimension lower bounds, extents, and **explicit
// strides** so that views can describe non-contiguous slices.  The
// indexing API mirrors fortran::Array exactly: ``r(i, j, ...)`` is the
// same expression in both.
//
// Used for three things:
//   * Pass-by-reference parameters in translated subprograms — every
//     ``intent(in[out])`` array parameter becomes an ArrayRef.
//   * Slicing — translated array sections (when we implement them)
//     produce ArrayRef instances whose strides may not be contiguous.
//   * EQUIVALENCE array overlay (D7) — ArrayRef wraps a slice of a
//     byte buffer with an offset and extent.
//
// ArrayRef is trivially copyable; passing it by value is the
// expected idiom.  ``const`` correctness uses the element type:
//   ArrayRef<int, 2>           — mutable view
//   ArrayRef<const int, 2>     — read-only view
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_RT_ARRAY_REF_HPP
#define FORTRAN_RT_ARRAY_REF_HPP

#include "array.hpp"

#include <array>
#include <cassert>
#include <cstddef>
#include <type_traits>

namespace fortran {

template <typename T, std::size_t Rank> class ArrayRef {
  static_assert(Rank >= 1, "fortran::ArrayRef rank must be >= 1");

public:
  using element_type = T;
  using value_type = std::remove_cv_t<T>;
  using extent_array = std::array<index_t, Rank>;
  using lower_array = std::array<index_t, Rank>;

  /// Empty / null view.  Indexing is undefined.
  ArrayRef() noexcept = default;

  /// View ``data`` interpreted as a Fortran array.  Lower bounds default
  /// to 1; strides default to column-major from extents (contiguous).
  ArrayRef(T *data, const extent_array &extents) noexcept
      : ArrayRef(data, lower_array_filled(1), extents,
                 detail::column_major_strides(extents)) {}

  /// View ``data`` with explicit lower bounds; strides default to
  /// column-major from extents (contiguous).
  ArrayRef(T *data, const lower_array &lower,
           const extent_array &extents) noexcept
      : ArrayRef(data, lower, extents,
                 detail::column_major_strides(extents)) {}

  /// Fully explicit view — base pointer, lower bounds, extents, and
  /// strides.  Used for non-contiguous slices.
  ArrayRef(T *data, const lower_array &lower, const extent_array &extents,
           const extent_array &strides) noexcept
      : data_(data), lower_(lower), extents_(extents), strides_(strides) {}

  // Conversion from an owning Array is provided by Array's implicit
  // operator ArrayRef<T, Rank>() (in array_ref.hpp at the bottom of
  // this file).  We do not also define the inverse converting
  // constructor here, to avoid an ambiguous overload at the call site.

  // ---- Indexing -------------------------------------------------------

  template <typename... Idx>
  T &operator()(Idx... idxs) const {
    return data_[detail::linear_offset<Rank>(lower_, upper(), strides_,
                                             idxs...)];
  }

  // ---- Bounds inspection ---------------------------------------------

  index_t lbound(std::size_t dim) const noexcept {
    assert(dim >= 1 && dim <= Rank);
    return lower_[dim - 1];
  }

  index_t ubound(std::size_t dim) const noexcept {
    assert(dim >= 1 && dim <= Rank);
    return lower_[dim - 1] + extents_[dim - 1] - 1;
  }

  index_t extent(std::size_t dim) const noexcept {
    assert(dim >= 1 && dim <= Rank);
    return extents_[dim - 1];
  }

  index_t size() const noexcept { return detail::total_size(extents_); }
  bool empty() const noexcept { return data_ == nullptr || size() == 0; }

  const lower_array &lower_bounds() const noexcept { return lower_; }
  const extent_array &extents() const noexcept { return extents_; }
  const extent_array &strides() const noexcept { return strides_; }

  T *data() const noexcept { return data_; }

  /// Rank-1 section view ``a(lo:hi:stride)`` as a new ArrayRef whose
  /// elements are 1-based.  Only valid on a rank-1 view.
  ArrayRef<T, 1> section(index_t lo, index_t hi, index_t stride = 1) const
      noexcept {
    static_assert(Rank == 1, "section(lo,hi,stride) is rank-1 only");
    const index_t n = stride != 0 ? (hi - lo) / stride + 1 : 0;
    T *base = &data_[(lo - lower_[0]) * strides_[0]];
    return ArrayRef<T, 1>(base, {index_t{1}}, {n < 0 ? index_t{0} : n},
                          {strides_[0] * stride});
  }

  /// Visit every element once.  Handles arbitrary (possibly
  /// non-contiguous) strides by walking the Fortran index tuple in
  /// column-major order.
  template <typename F> void for_each(F &&f) const {
    const index_t n = size();
    if (n == 0 || data_ == nullptr) {
      return;
    }
    std::array<index_t, Rank> idx = lower_;
    for (index_t count = 0; count < n; ++count) {
      index_t off = 0;
      for (std::size_t k = 0; k < Rank; ++k) {
        off += (idx[k] - lower_[k]) * strides_[k];
      }
      f(data_[off]);
      for (std::size_t k = 0; k < Rank; ++k) {
        if (++idx[k] <= lower_[k] + extents_[k] - 1) {
          break;
        }
        idx[k] = lower_[k];
      }
    }
  }

  /// True if this view's strides describe a contiguous column-major
  /// layout — i.e. data + N elements actually visits all elements in
  /// linear order.  Useful for the emitter when deciding whether a
  /// BLAS-style routine can take the data pointer directly.
  bool is_contiguous() const noexcept {
    const auto expected = detail::column_major_strides(extents_);
    return strides_ == expected;
  }

private:
  static lower_array lower_array_filled(index_t v) noexcept {
    lower_array out{};
    out.fill(v);
    return out;
  }

  extent_array upper() const noexcept {
    extent_array u{};
    for (std::size_t i = 0; i < Rank; ++i) {
      u[i] = lower_[i] + extents_[i] - 1;
    }
    return u;
  }

  T *data_{nullptr};
  lower_array lower_{};
  extent_array extents_{};
  extent_array strides_{};
};

// ---- Array <-> ArrayRef implicit conversions ------------------------------

template <typename T, std::size_t Rank>
Array<T, Rank>::operator ArrayRef<T, Rank>() noexcept {
  return ArrayRef<T, Rank>(data(), lower_, extents_, strides_);
}

template <typename T, std::size_t Rank>
Array<T, Rank>::operator ArrayRef<const T, Rank>() const noexcept {
  return ArrayRef<const T, Rank>(data(), lower_, extents_, strides_);
}

template <typename T, std::size_t Rank>
ArrayRef<T, 1> Array<T, Rank>::section(index_t lo, index_t hi,
                                       index_t stride) noexcept {
  static_assert(Rank == 1, "section(lo,hi,stride) is rank-1 only");
  return ArrayRef<T, Rank>(*this).section(lo, hi, stride);
}

} // namespace fortran

#endif // FORTRAN_RT_ARRAY_REF_HPP

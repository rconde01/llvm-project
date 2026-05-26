//===-- fortran/array.hpp - Custom column-major Fortran array ---*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// fortran::Array<T, Rank>
//
// Owning, move-only, column-major N-dimensional array with arbitrary
// per-dimension lower bounds, matching Fortran's storage layout and
// indexing exactly.  See ../README.md (rule R1 and decision D1) for the
// rationale.
//
//   fortran::Array<int, 2> a({3, 4});           // 1:3, 1:4
//   a(1, 1) = 42;                                // 1-based
//   a(3, 4) = 7;
//
//   fortran::Array<double, 1> b({-5}, {11});    // -5:5
//   b(-5) = 1.0;
//
// Bounds are runtime values; bounds checks are active when the macro
// FORTRAN_RT_BOUNDS_CHECK is defined (the default unless NDEBUG is set).
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_RT_ARRAY_HPP
#define FORTRAN_RT_ARRAY_HPP

#include <algorithm>
#include <array>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <type_traits>
#include <utility>

namespace fortran {

/// Signed integer type used everywhere for indices, bounds, and extents.
/// Signed so that arbitrary lower bounds (including negative ones) are
/// representable directly.
using index_t = std::ptrdiff_t;

/// One ranged subscript ``lo:hi:stride`` of a multi-dimensional array
/// section.  A section subscript is either a ``Slice`` (keeps the
/// dimension) or a plain integer index (drops it).
struct Slice {
  index_t lo;
  index_t hi;
  index_t stride;
  constexpr Slice(index_t lo_, index_t hi_, index_t stride_ = 1) noexcept
      : lo(lo_), hi(hi_), stride(stride_) {}
};

namespace detail {
template <typename X>
inline constexpr bool is_slice_v = std::is_same_v<std::decay_t<X>, Slice>;
} // namespace detail

/// Per-dimension bounds.  Both inclusive: ``a(lower)`` and ``a(upper)`` are
/// valid; the extent is ``upper - lower + 1``.
struct Bounds {
  index_t lower;
  index_t upper;
  constexpr index_t extent() const noexcept { return upper - lower + 1; }
};

// ----- internal helpers ----------------------------------------------------
namespace detail {

/// Compute column-major strides from extents.  stride[0] = 1; subsequent
/// strides are the running product of preceding extents.  This is what
/// makes ``a(1,1), a(2,1), a(3,1), a(1,2), ...`` contiguous in memory.
template <std::size_t Rank>
constexpr std::array<index_t, Rank>
column_major_strides(const std::array<index_t, Rank> &extents) noexcept {
  std::array<index_t, Rank> strides{};
  if constexpr (Rank > 0) {
    strides[0] = 1;
    for (std::size_t i = 1; i < Rank; ++i) {
      strides[i] = strides[i - 1] * extents[i - 1];
    }
  }
  return strides;
}

template <std::size_t Rank>
constexpr index_t
total_size(const std::array<index_t, Rank> &extents) noexcept {
  index_t n = 1;
  for (std::size_t i = 0; i < Rank; ++i) {
    n *= extents[i];
  }
  return n;
}

#if defined(FORTRAN_RT_BOUNDS_CHECK) || !defined(NDEBUG)
[[noreturn]] inline void bounds_error(const char *what, index_t idx,
                                      index_t lo, index_t hi) {
  // Use a runtime exception so that tests can observe the error and
  // generated code can choose to catch and continue.
  char buf[256];
  std::snprintf(buf, sizeof(buf),
                "fortran::Array: %s index %lld out of range [%lld, %lld]",
                what, static_cast<long long>(idx),
                static_cast<long long>(lo), static_cast<long long>(hi));
  throw std::out_of_range(buf);
}
#define FORTRAN_RT_CHECK_BOUNDS(idx, lo, hi, dim)                              \
  do {                                                                         \
    if ((idx) < (lo) || (idx) > (hi)) {                                        \
      ::fortran::detail::bounds_error(dim, (idx), (lo), (hi));                 \
    }                                                                          \
  } while (0)
#else
#define FORTRAN_RT_CHECK_BOUNDS(idx, lo, hi, dim) ((void)0)
#endif

/// Variadic offset computation:
///   sum_{k=0..R-1} (idx[k] - lower[k]) * stride[k]
/// Accepts any integral indices, converts each to ``index_t``.
template <std::size_t Rank, typename... Idx>
constexpr index_t linear_offset(
    const std::array<index_t, Rank> &lower,
    [[maybe_unused]] const std::array<index_t, Rank> &upper,
    const std::array<index_t, Rank> &strides, Idx... idxs) {
  static_assert(sizeof...(Idx) == Rank,
                "wrong number of indices for fortran::Array");
  static_assert((std::is_integral_v<Idx> && ...),
                "indices must be integral");
  const std::array<index_t, Rank> idx{static_cast<index_t>(idxs)...};
  index_t off = 0;
  for (std::size_t k = 0; k < Rank; ++k) {
    FORTRAN_RT_CHECK_BOUNDS(idx[k], lower[k], upper[k],
                            k == 0 ? "dim 1" :
                            k == 1 ? "dim 2" :
                            k == 2 ? "dim 3" : "dim N");
    off += (idx[k] - lower[k]) * strides[k];
  }
  return off;
}

} // namespace detail

// Forward declaration for the implicit conversion below.
template <typename T, std::size_t Rank> class ArrayRef;

/// Owning, move-only, column-major Fortran-style array.
template <typename T, std::size_t Rank> class Array {
  static_assert(Rank >= 1, "fortran::Array rank must be >= 1");

public:
  using value_type = T;
  using extent_array = std::array<index_t, Rank>;
  using lower_array = std::array<index_t, Rank>;
  static constexpr std::size_t rank = Rank;

  /// Default-constructed array is empty (size() == 0).  Indexing it is UB
  /// (or throws in checked mode) — provided so generated code can declare
  /// arrays whose size is known later.
  Array() noexcept = default;

  /// Construct with explicit extents; lower bounds default to 1 in every
  /// dimension (the Fortran default).
  explicit Array(const extent_array &extents) {
    lower_.fill(1);
    extents_ = extents;
    init_storage();
  }

  /// Construct with explicit lower bounds *and* extents.
  Array(const lower_array &lower, const extent_array &extents) {
    lower_ = lower;
    extents_ = extents;
    init_storage();
  }

  /// Construct with explicit lower bounds, extents, and a scalar broadcast
  /// to every element — Fortran ``real :: a(n) = 0.0``.  (Only this
  /// three-argument form is provided; a two-argument ``(extents, fill)``
  /// would be ambiguous with ``(lower, extents)``.)
  Array(const lower_array &lower, const extent_array &extents, const T &fill)
      : Array(lower, extents) {
    std::fill_n(storage_.get(), size(), fill);
  }

  // Move-only.  Copies are explicit via ``clone()`` so the cost is visible
  // at every call site (R1 / D1).  We define move explicitly (rather
  // than ``= default``) so the moved-from array is left in a fully
  // empty state: ``std::array`` is not move-aware on its own, so a
  // defaulted move would copy ``extents_`` and leave ``size()`` lying.
  Array(const Array &) = delete;
  Array &operator=(const Array &) = delete;
  Array(Array &&other) noexcept { swap(other); }
  Array &operator=(Array &&other) noexcept {
    Array tmp(std::move(other));
    swap(tmp);
    return *this;
  }
  ~Array() = default;

  /// Broadcast a scalar to every element (Fortran ``a = 0.0`` on a whole
  /// array).  In place — no allocation or temporary.  Whole-array
  /// assignments with a plain ``Name`` target are expanded into explicit
  /// loops by the converter; this covers the remaining target contexts
  /// (derived-type components, etc.) uniformly.
  Array &operator=(const T &scalar) {
    std::fill_n(storage_.get(), size(), scalar);
    return *this;
  }

  /// Elementwise copy from a (possibly differently-typed, possibly
  /// strided) view — Fortran ``a = b`` / ``a%c = b(:,j)`` where ``b`` is
  /// a dummy array or section.  Copies in place into this array's
  /// existing storage (shapes are assumed conformable, as Fortran
  /// requires); does not rebind.  Distinct from the deleted Array copy-
  /// assignment, so ``a = other_array`` still requires an explicit
  /// ``clone()`` (copy cost stays visible, D1).
  template <typename U>
  Array &operator=(const ArrayRef<U, Rank> &src) {
    const index_t n = size();
    for (index_t i = 0; i < n; ++i) {
      linear_at(i) = static_cast<T>(src.linear_at(i));
    }
    return *this;
  }

  /// Elementwise copy from a same-rank array of a *different* element
  /// type (Fortran ``a = b`` with a kind/type conversion, or a character
  /// constructor feeding a CHARACTER array).  Constrained to a different
  /// element type so the deleted same-type copy still forces an explicit
  /// ``clone()`` (D1); template deduction wouldn't apply the implicit
  /// Array->ArrayRef conversion, hence this dedicated overload.
  template <typename U>
    requires(!std::is_same_v<U, T>)
  Array &operator=(const Array<U, Rank> &src) {
    const index_t n = size();
    for (index_t i = 0; i < n; ++i) {
      linear_at(i) = static_cast<T>(src.linear_at(i));
    }
    return *this;
  }

  /// Fill a multi-dimensional array from a flat rank-1 list, in Fortran
  /// element (column-major) order — ``DATA a(2,3) /.../`` lowers to
  /// ``a = array_of({...})``, where the constructor is one-dimensional.
  /// (Rank-1 targets use the element-copy overload above.)
  template <typename U>
    requires(Rank != 1)
  Array &operator=(const Array<U, 1> &flat) {
    const index_t n = std::min(size(), flat.size());
    for (index_t i = 0; i < n; ++i) {
      linear_at(i) = static_cast<T>(flat.linear_at(i));
    }
    return *this;
  }

  /// Swap with another array of the same type.  Used by the move ops.
  void swap(Array &other) noexcept {
    using std::swap;
    swap(lower_, other.lower_);
    swap(extents_, other.extents_);
    swap(strides_, other.strides_);
    swap(storage_, other.storage_);
  }

  /// Release storage and become empty.  Models Fortran ``DEALLOCATE``;
  /// re-``allocate`` by move-assigning a freshly-sized Array.
  void deallocate() noexcept {
    Array empty;
    swap(empty);
  }

  /// Whether the array currently owns storage (Fortran ``ALLOCATED``).
  bool allocated() const noexcept { return static_cast<bool>(storage_); }

  /// Explicit deep copy.  Heap-allocates a fresh buffer of ``size()``
  /// elements and copies element-wise.
  Array clone() const {
    Array out{lower_, extents_};
    if (storage_) {
      std::copy_n(storage_.get(), size(), out.storage_.get());
    }
    return out;
  }

  // ---- Indexing --------------------------------------------------------

  /// ``a(i, j, k, ...)`` — accepts ``Rank`` integral indices, applies
  /// each dimension's lower bound, returns a reference to the element.
  template <typename... Idx>
  T &operator()(Idx... idxs) {
    return storage_[detail::linear_offset<Rank>(lower_, upper(), strides_,
                                                idxs...)];
  }

  template <typename... Idx>
  const T &operator()(Idx... idxs) const {
    return storage_[detail::linear_offset<Rank>(lower_, upper(), strides_,
                                                idxs...)];
  }

  // ---- Bounds inspection ----------------------------------------------

  /// 1-based dimension argument matches Fortran's ``LBOUND(a, dim)``.
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

  /// Total number of elements (product of extents).
  index_t size() const noexcept { return detail::total_size(extents_); }

  /// Whether the array currently owns storage.
  bool empty() const noexcept { return size() == 0; }

  /// Constant references to the full per-dimension arrays.
  const lower_array &lower_bounds() const noexcept { return lower_; }
  const extent_array &extents() const noexcept { return extents_; }
  const extent_array &strides() const noexcept { return strides_; }

  // ---- Raw access -----------------------------------------------------

  /// Pointer to the (column-major) underlying storage.  Suitable for
  /// passing to BLAS/LAPACK routines that take a column-major buffer
  /// and explicit leading-dimension extent.
  T *data() noexcept { return storage_.get(); }
  const T *data() const noexcept { return storage_.get(); }

  /// Element at 0-based column-major logical position ``k``.  Storage is
  /// contiguous column-major, so this is just ``data()[k]`` — provided so
  /// the elementwise operators can treat Array and ArrayRef uniformly.
  T &linear_at(index_t k) noexcept { return storage_[k]; }
  const T &linear_at(index_t k) const noexcept { return storage_[k]; }

  // ---- Whole-array operations ----------------------------------------

  /// Set every element to ``value``.  Equivalent to ``a = value`` in
  /// Fortran when ``a`` is an array.
  void fill(const T &value) {
    if (storage_) {
      std::fill_n(storage_.get(), size(), value);
    }
  }

  /// Visit every element once (column-major order).  Used by the
  /// array-reduction intrinsics (SUM, MAXVAL, ...).  Storage is
  /// contiguous, so this is a simple linear scan.
  template <typename F> void for_each(F &&f) const {
    const index_t n = size();
    for (index_t i = 0; i < n; ++i) {
      f(storage_[i]);
    }
  }

  // ---- Conversion to non-owning view ----------------------------------

  operator ArrayRef<T, Rank>() noexcept;
  operator ArrayRef<const T, Rank>() const noexcept;

  /// Fortran sequence association: a whole array passed to a rank-1
  /// (assumed-size) dummy shares its storage as one flat 1-D sequence.
  /// Storage is contiguous column-major, so the flat view *is* the
  /// storage order.  Guarded to ``Rank != 1`` so the same-rank
  /// conversion above still handles an ordinary rank-1 actual.
  template <std::size_t R = Rank>
    requires(R != 1)
  operator ArrayRef<T, 1>() noexcept;
  template <std::size_t R = Rank>
    requires(R != 1)
  operator ArrayRef<const T, 1>() const noexcept;

  /// Rank-1 section view ``a(lo:hi:stride)``.  Convenience that
  /// forwards to ArrayRef::section (defined in array_ref.hpp).
  ArrayRef<T, 1> section(index_t lo, index_t hi, index_t stride = 1) noexcept;
  ArrayRef<const T, 1> section(index_t lo, index_t hi,
                               index_t stride = 1) const noexcept;

  /// General multi-dimensional section ``a(s1, s2, ...)``; each subscript
  /// is a ``Slice`` (kept dimension) or an integer index (dropped).
  /// Constrained to at least one ``Slice`` so the rank-1 ``section(lo,
  /// hi, stride)`` overload still wins for plain integer arguments.
  /// Forwards to ArrayRef::section (defined in array_ref.hpp).  The
  /// ``const`` overloads keep sections of a ``const`` array (e.g. a
  /// module PARAMETER) read-only.
  template <typename... Subs>
    requires(... || detail::is_slice_v<Subs>)
  auto section(Subs... subs) noexcept;
  template <typename... Subs>
    requires(... || detail::is_slice_v<Subs>)
  auto section(Subs... subs) const noexcept;

private:
  void init_storage() {
    strides_ = detail::column_major_strides(extents_);
    const index_t n = size();
    if (n > 0) {
      // value-initialize (zero for arithmetic types) so generated code
      // has the same default as Fortran's behavior with -finit-zero etc.
      storage_ = std::make_unique<T[]>(static_cast<std::size_t>(n));
    }
  }

  extent_array upper() const noexcept {
    extent_array u{};
    for (std::size_t i = 0; i < Rank; ++i) {
      u[i] = lower_[i] + extents_[i] - 1;
    }
    return u;
  }

  lower_array lower_{};
  extent_array extents_{};
  extent_array strides_{};
  std::unique_ptr<T[]> storage_{};
};

} // namespace fortran

#endif // FORTRAN_RT_ARRAY_HPP

//===-- fortran/array.hpp - Custom column-major Fortran array ---*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// ftn::Array<T, Rank, Lower>
//
// Owning, move-only, column-major N-dimensional array with arbitrary
// per-dimension lower bounds, matching Fortran's storage layout and
// indexing exactly.  See ../README.md (rule R1 and decision D1) for the
// rationale.
//
//   ftn::Array<int, 2> a({3, 4});           // 1:3, 1:4 (default lb=1)
//   a(1, 1) = 42;                                // 1-based
//   a(3, 4) = 7;
//
//   ftn::Array<double, 1> b({-5}, {11});    // -5:5, runtime bounds
//   b(-5) = 1.0;
//
//   // Compile-time bounds via the third template argument.  The lower
//   // bound becomes part of the type and is constant-folded into the
//   // indexing math instead of being read from a member each access.
//   //
//   //   ftn::Array<float, 1, std::array<index_t, 1>{0}> c({10});  // 0:9
//   //
//   // Defaults to the runtime sentinel ``Lower = detail::runtime_lower``,
//   // so every existing ``Array<T, R>`` keeps its runtime-stored bound.
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
#include <limits>
#include <memory>
#include <stdexcept>
#include <string_view>
#include <type_traits>
#include <utility>

namespace ftn {

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

/// Sentinel value used in the default ``Array``/``ArrayRef`` ``Lower``
/// NTTP to mean "the lower bound for this dimension is supplied at
/// construction and stored in the object, not encoded in the type".
/// Indexing math reads the stored value in that case; when ``Lower``
/// holds concrete integers (none equal to ``kRuntimeLBound``), the
/// values are constant-folded into the indexing arithmetic instead.
inline constexpr index_t kRuntimeLBound =
    std::numeric_limits<index_t>::min() / 2;

/// Default ``Lower`` template argument: ``{kRuntimeLBound, ...}`` so the
/// type identifies "runtime bounds for every dimension" — the form
/// every existing ``Array<T, Rank>`` defaults to.
template <std::size_t Rank>
constexpr std::array<index_t, Rank> runtime_lower() noexcept {
  std::array<index_t, Rank> a{};
  a.fill(kRuntimeLBound);
  return a;
}

/// True if every entry of ``a`` is a concrete bound (no sentinel).
template <std::size_t Rank>
constexpr bool is_static_lower(const std::array<index_t, Rank> &a) noexcept {
  for (std::size_t i = 0; i < Rank; ++i) {
    if (a[i] == kRuntimeLBound) {
      return false;
    }
  }
  return true;
}

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

/// Extent used for an assumed-size view that has no known upper bound --
/// a scalar actual sequence-associated with an array dummy (the callee
/// supplies the real size via its own indexing).  Large enough that a
/// debug bounds check never rejects a realistic Fortran index, yet far
/// from ``index_t`` overflow so ``lower + extent - 1`` and offset math
/// stay well-defined.
inline constexpr index_t kAssumedExtent = index_t{1} << 40;

template <std::size_t Rank>
constexpr index_t
total_size(const std::array<index_t, Rank> &extents) noexcept {
  index_t n = 1;
  for (std::size_t i = 0; i < Rank; ++i) {
    // An assumed-size dimension (a scalar sequence-associated with an array
    // dummy) has no known extent; its huge sentinel must not blow up
    // ``size()`` into a runaway loop bound.  Count it as 1 -- the same
    // ``size() == 1`` a scalar-base view reported before assumed-size
    // bounds leniency.  A real array never carries this extent.
    n *= (extents[i] == kAssumedExtent) ? index_t{1} : extents[i];
  }
  return n;
}

#if defined(FORTRAN_RT_BOUNDS_CHECK) || !defined(NDEBUG)
/// True when subscript bounds checking is compiled in.  When false, the
/// index path needs neither the per-dimension upper bound nor the
/// ``upper()`` computation that feeds it -- letting the caller skip that
/// dead work entirely (measured ~2% of a compute-bound family otherwise).
inline constexpr bool kBoundsCheck = true;
[[noreturn]] inline void bounds_error(const char *what, index_t idx,
                                      index_t lo, index_t hi) {
  // Use a runtime exception so that tests can observe the error and
  // generated code can choose to catch and continue.
  char buf[256];
  std::snprintf(buf, sizeof(buf),
                "ftn::Array: %s index %lld out of range [%lld, %lld]",
                what, static_cast<long long>(idx),
                static_cast<long long>(lo), static_cast<long long>(hi));
  throw std::out_of_range(buf);
}
#define FORTRAN_RT_CHECK_BOUNDS(idx, lo, hi, dim)                              \
  do {                                                                         \
    if ((idx) < (lo) || (idx) > (hi)) {                                        \
      ::ftn::detail::bounds_error(dim, (idx), (lo), (hi));                 \
    }                                                                          \
  } while (0)
#else
inline constexpr bool kBoundsCheck = false;
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
                "wrong number of indices for ftn::Array");
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

/// Same as above but with the lower bound supplied as a compile-time
/// ``std::array`` NTTP, so the ``(idx[k] - Lower[k])`` subtraction
/// constant-folds and a typical inlined ``a(i, j)`` no longer reads a
/// per-array lower-bounds buffer on the hot path.  Bounds-check ``upper``
/// stays runtime (extents are runtime even when lb is fixed).
template <std::size_t Rank, std::array<index_t, Rank> Lower, typename... Idx>
constexpr index_t linear_offset_static(
    [[maybe_unused]] const std::array<index_t, Rank> &upper,
    const std::array<index_t, Rank> &strides, Idx... idxs) {
  static_assert(sizeof...(Idx) == Rank,
                "wrong number of indices for ftn::Array");
  static_assert((std::is_integral_v<Idx> && ...),
                "indices must be integral");
  const std::array<index_t, Rank> idx{static_cast<index_t>(idxs)...};
  index_t off = 0;
  for (std::size_t k = 0; k < Rank; ++k) {
    FORTRAN_RT_CHECK_BOUNDS(idx[k], Lower[k], upper[k],
                            k == 0 ? "dim 1" :
                            k == 1 ? "dim 2" :
                            k == 2 ? "dim 3" : "dim N");
    off += (idx[k] - Lower[k]) * strides[k];
  }
  return off;
}

// Contiguous (column-major) offset without a materialized strides array:
// the stride of dimension ``k`` is the running product of the leading
// extents.  Used by a ``Contiguous`` ArrayRef, which stores no ``strides_``
// -- one pass, no per-index array copy (so it's no slower than the stored
// form even at ``-O0``, and folds to a constant at ``-O2``).
template <std::size_t Rank, typename... Idx>
constexpr index_t linear_offset_contig(
    const std::array<index_t, Rank> &lower,
    [[maybe_unused]] const std::array<index_t, Rank> &upper,
    const std::array<index_t, Rank> &extents, Idx... idxs) {
  static_assert(sizeof...(Idx) == Rank, "wrong number of indices");
  static_assert((std::is_integral_v<Idx> && ...), "indices must be integral");
  const std::array<index_t, Rank> idx{static_cast<index_t>(idxs)...};
  index_t off = 0;
  index_t stride = 1;
  for (std::size_t k = 0; k < Rank; ++k) {
    FORTRAN_RT_CHECK_BOUNDS(idx[k], lower[k], upper[k],
                            k == 0 ? "dim 1" :
                            k == 1 ? "dim 2" :
                            k == 2 ? "dim 3" : "dim N");
    off += (idx[k] - lower[k]) * stride;
    stride *= extents[k];
  }
  return off;
}
template <std::size_t Rank, std::array<index_t, Rank> Lower, typename... Idx>
constexpr index_t linear_offset_contig_static(
    [[maybe_unused]] const std::array<index_t, Rank> &upper,
    const std::array<index_t, Rank> &extents, Idx... idxs) {
  static_assert(sizeof...(Idx) == Rank, "wrong number of indices");
  static_assert((std::is_integral_v<Idx> && ...), "indices must be integral");
  const std::array<index_t, Rank> idx{static_cast<index_t>(idxs)...};
  index_t off = 0;
  index_t stride = 1;
  for (std::size_t k = 0; k < Rank; ++k) {
    FORTRAN_RT_CHECK_BOUNDS(idx[k], Lower[k], upper[k],
                            k == 0 ? "dim 1" :
                            k == 1 ? "dim 2" :
                            k == 2 ? "dim 3" : "dim N");
    off += (idx[k] - Lower[k]) * stride;
    stride *= extents[k];
  }
  return off;
}

} // namespace detail

// Forward declaration for the implicit conversion below.  The defaults
// for ``Lower`` and ``Contiguous`` are provided here once; the full
// declaration in array_ref.hpp must NOT repeat them (a default argument
// can be supplied at most once across all redeclarations of a class
// template).  ``Contiguous`` == true drops the per-dimension ``strides_``
// member (they are column-major-derivable from the extents); a strided
// section view keeps ``Contiguous`` == false so its real strides survive.
template <typename T, std::size_t Rank,
          std::array<index_t, Rank> Lower = detail::runtime_lower<Rank>(),
          bool Contiguous = false>
class ArrayRef;

/// Owning, move-only, column-major Fortran-style array.
///
/// ``Lower`` is a non-type template parameter carrying the per-dimension
/// lower bound.  Its default ``runtime_lower<Rank>()`` means "lower
/// bounds are supplied at construction and stored in the object" — the
/// behavior every existing call site relies on.  When the caller spells
/// concrete integers (e.g. ``Array<float, 1, std::array<index_t,1>{0}>``)
/// the bounds become part of the type and constant-fold into indexing.
template <typename T, std::size_t Rank,
          std::array<index_t, Rank> Lower = detail::runtime_lower<Rank>()>
class Array {
  static_assert(Rank >= 1, "ftn::Array rank must be >= 1");

public:
  using value_type = T;
  using extent_array = std::array<index_t, Rank>;
  using lower_array = std::array<index_t, Rank>;
  static constexpr std::size_t rank = Rank;
  /// True when every dimension's lower bound is a compile-time constant
  /// (``Lower`` carries no ``kRuntimeLBound`` entries).
  static constexpr bool kStaticLower = detail::is_static_lower<Rank>(Lower);
  /// The compile-time lower bounds (only meaningful when ``kStaticLower``).
  static constexpr std::array<index_t, Rank> static_lower = Lower;

  /// Default-constructed array is empty (size() == 0).  Indexing it is UB
  /// (or throws in checked mode) — provided so generated code can declare
  /// arrays whose size is known later.
  Array() noexcept = default;

  /// Construct with explicit extents.  When ``kStaticLower`` is true the
  /// per-dimension lower bounds come from ``Lower``; otherwise they
  /// default to 1 (the Fortran default).
  explicit Array(const extent_array &extents) {
    if constexpr (kStaticLower) {
      lower_ = Lower;
    } else {
      lower_.fill(1);
    }
    extents_ = extents;
    init_storage();
  }

  /// Construct with extents and a scalar broadcast to every element —
  /// Fortran ``real :: a(n) = 0.0``.  Available only when ``kStaticLower``
  /// is true; for runtime bounds the three-argument
  /// ``(lower, extents, fill)`` form must be used (a two-argument
  /// ``(extents, fill)`` would be ambiguous with ``(lower, extents)``).
  Array(const extent_array &extents, const T &fill)
    requires(kStaticLower)
      : Array(extents) {
    std::fill_n(storage_.get(), size(), fill);
  }

  /// Construct with explicit lower bounds *and* extents.  Only available
  /// when bounds aren't already fixed at the type level; for a
  /// static-``Lower`` instantiation use the ``(extents)`` overload.
  Array(const lower_array &lower, const extent_array &extents)
    requires(!kStaticLower)
  {
    lower_ = lower;
    extents_ = extents;
    init_storage();
  }

  /// Construct with explicit lower bounds, extents, and a scalar broadcast
  /// to every element — Fortran ``real :: a(n) = 0.0`` with non-default
  /// lower bounds.
  Array(const lower_array &lower, const extent_array &extents, const T &fill)
    requires(!kStaticLower)
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
  template <typename U, std::array<index_t, Rank> L2, bool C2>
  Array &operator=(const ArrayRef<U, Rank, L2, C2> &src) {
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
  template <typename U, std::array<index_t, Rank> SrcLower>
    requires(!std::is_same_v<U, T>)
  Array &operator=(const Array<U, Rank, SrcLower> &src) {
    // Bound by the source extent too: a shorter source (e.g. a partially
    // specified DATA list) must not be read past its end.
    const index_t n = std::min(size(), src.size());
    for (index_t i = 0; i < n; ++i) {
      if constexpr (std::is_integral_v<T> &&
                    std::is_convertible_v<U, std::string_view>) {
        // Fortran CHARACTER -> INTEGER type-pun: a numeric COMMON slot
        // aliased as CHARACTER (or a Hollerith assignment) receives the
        // character bytes verbatim, blank-padded to the integer's width --
        // the array analogue of FortranString's ``operator I()``.  (E.g.
        // NRLMSISE-00's ISDATE/ISTIME/NAME in /DATIM7/.)
        std::string_view s{src.linear_at(i)};
        T val{};
        auto *bytes = reinterpret_cast<unsigned char *>(&val);
        for (std::size_t k = 0; k < sizeof(T); ++k) {
          bytes[k] = k < s.size() ? static_cast<unsigned char>(s[k])
                                  : static_cast<unsigned char>(' ');
        }
        linear_at(i) = val;
      } else {
        linear_at(i) = static_cast<T>(src.linear_at(i));
      }
    }
    return *this;
  }

  /// Elementwise copy from a same-rank array of the *same* element type
  /// but a *different* ``Lower`` (the destination is a static-bound
  /// SAVE struct field, the source is an Array constructor result with
  /// the runtime-sentinel Lower).  Without this, ``a = array_of(...)``
  /// where ``a`` is static-lb has no matching operator=.  Distinct from
  /// the deleted same-type-same-Lower copy assignment.
  template <std::array<index_t, Rank> SrcLower>
    requires(SrcLower != Lower)
  Array &operator=(const Array<T, Rank, SrcLower> &src) {
    const index_t n = std::min(size(), src.size());
    for (index_t i = 0; i < n; ++i) {
      linear_at(i) = src.linear_at(i);
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

  /// Bulk-fill from a flat C-array of compile-time-known length, in
  /// Fortran column-major order.  The element count ``N`` is deduced from
  /// the buffer, so a generated ``DATA`` initializer reads as
  /// ``a.assign_data(a_data)`` where ``a_data`` is a ``static constexpr``
  /// table -- the values live in read-only storage and the fill is one
  /// loop, instead of a giant inline ``array_of(v0, v1, ...)`` varargs
  /// call.  Copies ``min(N, size())`` elements (an array may be partly
  /// initialized).  Returns ``*this`` so it can chain.
  template <typename U, std::size_t N>
  Array &assign_data(const U (&src)[N]) {
    const index_t n = std::min<index_t>(size(), static_cast<index_t>(N));
    for (index_t i = 0; i < n; ++i) {
      linear_at(i) = static_cast<T>(src[i]);
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
  /// When ``kStaticLower`` is true the lower-bound subtractions are
  /// constant-folded out of the offset math; otherwise the runtime
  /// ``lower_`` buffer is consulted on each access.
  template <typename... Idx>
  T &operator()(Idx... idxs) {
    if constexpr (kStaticLower) {
      return storage_[detail::linear_offset_static<Rank, Lower>(
          detail::kBoundsCheck ? upper() : std::array<index_t, Rank>{},
          strides_, idxs...)];
    } else {
      return storage_[detail::linear_offset<Rank>(
          lower_, detail::kBoundsCheck ? upper() : std::array<index_t, Rank>{},
          strides_, idxs...)];
    }
  }

  template <typename... Idx>
  const T &operator()(Idx... idxs) const {
    if constexpr (kStaticLower) {
      return storage_[detail::linear_offset_static<Rank, Lower>(
          detail::kBoundsCheck ? upper() : std::array<index_t, Rank>{},
          strides_, idxs...)];
    } else {
      return storage_[detail::linear_offset<Rank>(
          lower_, detail::kBoundsCheck ? upper() : std::array<index_t, Rank>{},
          strides_, idxs...)];
    }
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

  /// Implicit conversion to a non-owning view.  Templated on the
  /// destination ``Lower`` so an Array with one lb (static or runtime)
  /// can be passed to a dummy declared with a different lb -- the
  /// Fortran rule that the dummy's declared lb is what the callee
  /// indexes against.  When the destination is static the runtime
  /// ``lower_`` field on the view is initialized from the destination's
  /// ``Lower``; the source's lb only matters for the ``lower_bounds()``
  /// reading on the source itself.
  // An owning Array is always contiguous, so it can convert to either a
  // strided (``DstCon`` == false) or a contiguous (``true``) view directly
  // -- the latter lets a whole array bind a ``Contiguous`` dummy in one
  // user-defined conversion (chaining two would be ill-formed).
  template <std::array<index_t, Rank> DstLower = detail::runtime_lower<Rank>(),
            bool DstCon = false>
  operator ArrayRef<T, Rank, DstLower, DstCon>() noexcept;
  template <std::array<index_t, Rank> DstLower = detail::runtime_lower<Rank>(),
            bool DstCon = false>
  operator ArrayRef<const T, Rank, DstLower, DstCon>() const noexcept;

  /// Fortran sequence association: a whole array passed to a rank-1
  /// (assumed-size) dummy shares its storage as one flat 1-D sequence.
  /// Storage is contiguous column-major, so the flat view *is* the
  /// storage order.  Guarded to ``Rank != 1`` so the same-rank
  /// conversion above still handles an ordinary rank-1 actual.
  template <std::size_t R = Rank,
            std::array<index_t, 1> DstLower = detail::runtime_lower<1>(),
            bool DstCon = false>
    requires(R != 1)
  operator ArrayRef<T, 1, DstLower, DstCon>() noexcept;
  template <std::size_t R = Rank,
            std::array<index_t, 1> DstLower = detail::runtime_lower<1>(),
            bool DstCon = false>
    requires(R != 1)
  operator ArrayRef<const T, 1, DstLower, DstCon>() const noexcept;

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

} // namespace ftn

#endif // FORTRAN_RT_ARRAY_HPP

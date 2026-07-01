//===-- fortran/array_ref.hpp - Non-owning array view -----------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// ftn::ArrayRef<T, Rank>
//
// A non-owning view of an N-dimensional Fortran-style array.  Stores a
// base pointer, per-dimension lower bounds, extents, and **explicit
// strides** so that views can describe non-contiguous slices.  The
// indexing API mirrors ftn::Array exactly: ``r(i, j, ...)`` is the
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
#include "string.hpp"

#include <algorithm>
#include <array>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <format>
#include <istream>
#include <ostream>
#include <tuple>
#include <type_traits>
#include <utility>

namespace ftn {

// Default for ``Lower`` is supplied on the forward declaration in
// array.hpp; do not repeat it here (C++ allows a default template
// argument to be given only once across redeclarations).
template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
class ArrayRef {
  static_assert(Rank >= 1, "ftn::ArrayRef rank must be >= 1");

public:
  using element_type = T;
  using value_type = std::remove_cv_t<T>;
  using extent_array = std::array<index_t, Rank>;
  using lower_array = std::array<index_t, Rank>;
  static constexpr std::size_t rank = Rank;
  /// True when every dimension's lower bound is a compile-time constant
  /// (``Lower`` carries no ``kRuntimeLBound`` entries) -- mirrors
  /// ``Array<T, Rank, Lower>::kStaticLower``.
  static constexpr bool kStaticLower = detail::is_static_lower<Rank>(Lower);
  static constexpr std::array<index_t, Rank> static_lower = Lower;

  /// Empty / null view.  Indexing is undefined.
  ArrayRef() noexcept = default;

  /// View ``data`` interpreted as a Fortran array.  Lower bounds default
  /// to ``Lower`` when static, else to 1; strides default to column-major
  /// from extents (contiguous).
  ArrayRef(T *data, const extent_array &extents) noexcept
      : ArrayRef(data, default_lower_bounds(), extents,
                 detail::column_major_strides(extents)) {}

  /// View ``data`` with explicit lower bounds; strides default to
  /// column-major from extents (contiguous).  Only available when the
  /// type's ``Lower`` is the runtime sentinel; for a static ``Lower`` the
  /// (``data``, ``extents``) overload already uses the template-provided
  /// bound.
  ArrayRef(T *data, const lower_array &lower,
           const extent_array &extents) noexcept
    requires(!kStaticLower)
      : ArrayRef(data, lower, extents,
                 detail::column_major_strides(extents)) {}

  /// Fully explicit view — base pointer, lower bounds, extents, and
  /// strides.  Used for non-contiguous slices.  Always available so the
  /// internal section / conversion code can rebuild views with a static
  /// ``Lower`` (the caller is responsible for the runtime ``lower`` then
  /// matching ``Lower``; the lookup inside ``operator()`` reads from
  /// ``Lower`` regardless).
  ArrayRef(T *data, const lower_array &lower, const extent_array &extents,
           const extent_array &strides) noexcept
      : data_(data), lower_(lower), extents_(extents), strides_(strides) {}

  // Conversion from an owning Array is provided by Array's implicit
  // operator ArrayRef<T, Rank>() (in array_ref.hpp at the bottom of
  // this file).  We do not also define the inverse converting
  // constructor here, to avoid an ambiguous overload at the call site.

  /// Single converting constructor that handles two shifts at once:
  ///
  ///   * **const-add:** ``ArrayRef<T>`` -> ``ArrayRef<const T>`` so a
  ///     mutable view binds where a read-only view is expected.
  ///   * **Lower-rebind:** ``ArrayRef<T, R, L1>`` -> ``ArrayRef<T, R,
  ///     L2>`` so a caller's view with one lower bound binds to a
  ///     callee's dummy declared with another (Fortran's "the dummy's
  ///     declared lb, not the actual's, is what indexing uses").
  ///
  /// The default copy-constructor still wins for the identity case
  /// (same ``T``, same ``Lower``), so this overload only fires when at
  /// least one of the two shifts is real.
  template <typename U, std::array<index_t, Rank> OtherLower>
    requires((std::is_same_v<T, U> ||
              (std::is_const_v<T> &&
               std::is_same_v<std::remove_const_t<T>, U>)) &&
             !(std::is_same_v<T, U> && OtherLower == Lower))
  ArrayRef(const ArrayRef<U, Rank, OtherLower> &other) noexcept
      : data_(other.data()),
        lower_(kStaticLower ? Lower : other.lower_bounds()),
        extents_(other.extents()), strides_(other.strides()) {}

  /// Fortran storage (sequence) association: a scalar actual passed to an
  /// array dummy is the *base* of that dummy.  The callee indexes the dummy
  /// per its own declared size (e.g. ``CTR(CTRSIZ)`` reads ``CTR(2)``),
  /// relying on the actual's storage extending that far -- a classic F77
  /// idiom (a counter array's first element, or an element passed for a
  /// whole row).  The scalar alone gives no extent, so the view is treated
  /// as assumed-size: an effectively unbounded extent, so debug bounds
  /// checks don't reject the (valid, caller-provided) adjacent storage.
  ArrayRef(T &scalar) noexcept
      : ArrayRef(&scalar, lower_array_filled(detail::kAssumedExtent)) {}

  /// Same, for a ``const`` or rvalue scalar actual (an intent(in) value,
  /// a literal, or an expression) — read in the callee, valid for the
  /// call.  Disabled when the element is already ``const`` (the overload
  /// above covers it) to avoid a redeclaration.
  template <typename U = T>
    requires(!std::is_const_v<U>)
  ArrayRef(const T &scalar) noexcept
      : ArrayRef(const_cast<T *>(&scalar),
                 lower_array_filled(detail::kAssumedExtent)) {}

  /// Sequence association from a higher-rank view: flatten to a 1-D view
  /// over the contiguous storage (extent = total element count).  Rank-1
  /// target only; mirrors the Array<T,Rank> -> ArrayRef<T,1> conversion.
  /// Source lower bound is irrelevant to a flatten (the bytes are the same
  /// either way), so accept any ``SrcLower`` -- a static-lb 2-D dummy
  /// (``POOL(2, LBPOOL:*)``) still flattens to a rank-1 view.
  template <std::size_t R2, std::array<index_t, R2> SrcLower>
    requires(Rank == 1 && R2 != 1)
  ArrayRef(const ArrayRef<T, R2, SrcLower> &other) noexcept
      : ArrayRef(other.data(), extent_array{{other.size()}}) {}

  // ---- Indexing -------------------------------------------------------

  template <typename... Idx>
  T &operator()(Idx... idxs) const {
    if constexpr (kStaticLower) {
      return data_[detail::linear_offset_static<Rank, Lower>(
          upper(), strides_, idxs...)];
    } else {
      return data_[detail::linear_offset<Rank>(lower_, upper(), strides_,
                                               idxs...)];
    }
  }

  // ---- Bounds inspection ---------------------------------------------

  index_t lbound(std::size_t dim) const noexcept {
    assert(dim >= 1 && dim <= Rank);
    if constexpr (kStaticLower) {
      return Lower[dim - 1];
    } else {
      return lower_[dim - 1];
    }
  }

  index_t ubound(std::size_t dim) const noexcept {
    assert(dim >= 1 && dim <= Rank);
    if constexpr (kStaticLower) {
      return Lower[dim - 1] + extents_[dim - 1] - 1;
    } else {
      return lower_[dim - 1] + extents_[dim - 1] - 1;
    }
  }

  index_t extent(std::size_t dim) const noexcept {
    assert(dim >= 1 && dim <= Rank);
    return extents_[dim - 1];
  }

  index_t size() const noexcept { return detail::total_size(extents_); }
  bool empty() const noexcept { return data_ == nullptr || size() == 0; }

  /// For an OPTIONAL array dummy, ``PRESENT(a)`` lowers to
  /// ``a.has_value()``: an absent optional array is passed as a null
  /// (default-constructed) view.
  bool has_value() const noexcept { return data_ != nullptr; }

  const lower_array &lower_bounds() const noexcept { return lower_; }
  const extent_array &extents() const noexcept { return extents_; }
  const extent_array &strides() const noexcept { return strides_; }

  T *data() const noexcept { return data_; }

  /// Broadcast a scalar to every viewed element (Fortran ``a(i:j) = 0``).
  /// Writes through the view; ``const`` because it mutates the pointed-to
  /// data, not the view itself (so it also binds to a section rvalue).
  const ArrayRef &operator=(const T &scalar) const {
    for (index_t i = 0; i < size(); ++i) {
      linear_at(i) = scalar;
    }
    return *this;
  }

  /// Copy an array-valued result into the viewed elements (Fortran
  /// ``a(lo:hi) = matmul(...)``).  Writes through the view, with element
  /// conversion; shapes are assumed conformable.  The same-element-type
  /// overload is a non-template, and these are intentionally *non-const*
  /// (like the implicit move-assignment) so that for an ``Array`` source
  /// — which would otherwise convert to an ``ArrayRef`` and pick the
  /// rebinding move-assignment — this element-copy is the unambiguous
  /// best match.  (Non-ref-qualified, so it still binds a section rvalue.)
  const ArrayRef &operator=(const Array<value_type, Rank> &src) {
    for (index_t i = 0; i < size(); ++i) {
      linear_at(i) = src.linear_at(i);
    }
    return *this;
  }
  template <typename U>
  const ArrayRef &operator=(const Array<U, Rank> &src) {
    for (index_t i = 0; i < size(); ++i) {
      linear_at(i) = static_cast<T>(src.linear_at(i));
    }
    return *this;
  }
  /// Whole-array copy from another ArrayRef (a section or another view).
  /// Fortran ``a = b(:,:,k)`` where both ``a`` and ``b(:,:,k)`` are array
  /// expressions: copy element-by-element through both views' strides.
  /// Without this overload, the compiler-generated copy assignment would
  /// silently *rebind* this view's pointer to the source's storage --
  /// the IRI ``read_data_SD`` pattern where ``coeff_month`` is a routine
  /// dummy and a section of a 3-D save array is the source.
  const ArrayRef &operator=(const ArrayRef &src) {
    for (index_t i = 0; i < size(); ++i) {
      linear_at(i) = src.linear_at(i);
    }
    return *this;
  }
  template <typename U>
  const ArrayRef &operator=(const ArrayRef<U, Rank> &src) {
    for (index_t i = 0; i < size(); ++i) {
      linear_at(i) = static_cast<T>(src.linear_at(i));
    }
    return *this;
  }
  /// Bulk-fill the viewed elements from a flat C-array of compile-time
  /// length, in Fortran column-major order -- the ArrayRef counterpart of
  /// ``Array::assign_data``.  Used when a ``DATA``-initialized array is an
  /// EQUIVALENCE/COMMON view rather than an owning ``Array`` (e.g. MSIS's
  /// ``pt1`` aliasing a slice of the ``parm`` block).  ``const`` because it
  /// writes through the view, not to the view itself.  Copies
  /// ``min(N, size())`` elements and returns ``*this`` so it can chain.
  template <typename U, std::size_t N>
  const ArrayRef &assign_data(const U (&src)[N]) const {
    const index_t n = std::min<index_t>(size(), static_cast<index_t>(N));
    for (index_t i = 0; i < n; ++i) {
      linear_at(i) = static_cast<T>(src[i]);
    }
    return *this;
  }

  /// Rebind this view's metadata (pointer, bounds, strides) to ``src``'s
  /// storage -- the Fortran POINTER associate ``p => target(...)``.
  /// Element-wise ``=`` copies data; rebind shares it.
  void rebind(const ArrayRef &src) noexcept {
    data_ = src.data_;
    lower_ = src.lower_;
    extents_ = src.extents_;
    strides_ = src.strides_;
  }

  /// Element at 0-based column-major logical position ``k``, honoring this
  /// view's (possibly non-contiguous) strides.  Lets the elementwise
  /// operators treat Array and ArrayRef uniformly even for sections.
  T &linear_at(index_t k) const noexcept {
    index_t off = 0;
    index_t rem = k;
    for (std::size_t d = 0; d < Rank; ++d) {
      off += (rem % extents_[d]) * strides_[d];
      rem /= extents_[d];
    }
    return data_[off];
  }

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

  /// General multi-dimensional section ``a(s1, s2, ...)`` where each
  /// subscript is either a ``Slice`` (a kept, ranged dimension) or an
  /// integer index (a dropped dimension).  The result rank is the number
  /// of ``Slice`` subscripts; result dimensions are 1-based.
  template <typename... Subs>
    requires(... || detail::is_slice_v<Subs>)
  auto section(Subs... subs) const noexcept {
    static_assert(sizeof...(Subs) == Rank,
                  "section needs one subscript per dimension");
    constexpr std::size_t NR =
        (std::size_t{0} + ... + (detail::is_slice_v<Subs> ? 1 : 0));
    static_assert(NR >= 1, "a section must keep at least one dimension");
    const std::tuple<Subs...> t{subs...};
    index_t offset = 0;
    std::array<index_t, NR> new_lower{};
    std::array<index_t, NR> new_extent{};
    std::array<index_t, NR> new_stride{};
    std::size_t ri = 0;
    const auto handle = [&](auto ic) {
      constexpr std::size_t k = decltype(ic)::value;
      const auto &sub = std::get<k>(t);
      if constexpr (detail::is_slice_v<std::tuple_element_t<
                        k, std::tuple<Subs...>>>) {
        offset += (sub.lo - lower_[k]) * strides_[k];
        const index_t n =
            sub.stride != 0 ? (sub.hi - sub.lo) / sub.stride + 1 : 0;
        new_lower[ri] = 1;
        new_extent[ri] = n < 0 ? index_t{0} : n;
        new_stride[ri] = strides_[k] * sub.stride;
        ++ri;
      } else {
        offset += (static_cast<index_t>(sub) - lower_[k]) * strides_[k];
      }
    };
    [&]<std::size_t... Is>(std::index_sequence<Is...>) {
      (handle(std::integral_constant<std::size_t, Is>{}), ...);
    }(std::make_index_sequence<Rank>{});
    return ArrayRef<T, NR>(data_ + offset, new_lower, new_extent, new_stride);
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

  /// Default per-dimension lower bounds: ``Lower`` when static, all-1s
  /// when the runtime sentinel is in effect.  Used by the
  /// ``(data, extents)`` ctor so a static-``Lower`` view's stored
  /// ``lower_`` mirrors the template parameter.
  static lower_array default_lower_bounds() noexcept {
    if constexpr (kStaticLower) {
      return Lower;
    } else {
      return lower_array_filled(1);
    }
  }

  extent_array upper() const noexcept {
    extent_array u{};
    for (std::size_t i = 0; i < Rank; ++i) {
      if constexpr (kStaticLower) {
        u[i] = Lower[i] + extents_[i] - 1;
      } else {
        u[i] = lower_[i] + extents_[i] - 1;
      }
    }
    return u;
  }

  T *data_{nullptr};
  lower_array lower_{};
  extent_array extents_{};
  extent_array strides_{};
};

// ---- Array <-> ArrayRef implicit conversions ------------------------------
//
// Each Array->ArrayRef conversion is templated on the destination
// ``Lower``, so an array with one lb (static or runtime) binds to a
// dummy declared with a different lb -- Fortran's "dummy's declared
// lb wins" rule, in C++ form.

template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
template <std::array<index_t, Rank> DstLower>
Array<T, Rank, Lower>::operator ArrayRef<T, Rank, DstLower>() noexcept {
  return ArrayRef<T, Rank, DstLower>(data(), lower_, extents_, strides_);
}

template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
template <std::array<index_t, Rank> DstLower>
Array<T, Rank, Lower>::operator ArrayRef<const T, Rank, DstLower>()
    const noexcept {
  return ArrayRef<const T, Rank, DstLower>(data(), lower_, extents_, strides_);
}

// Sequence association: flatten a higher-rank array to a rank-1 view over
// its contiguous storage (extent = total element count, lower bound 1).
template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
template <std::size_t R, std::array<index_t, 1> DstLower>
  requires(R != 1)
Array<T, Rank, Lower>::operator ArrayRef<T, 1, DstLower>() noexcept {
  return ArrayRef<T, 1, DstLower>(data(), std::array<index_t, 1>{{size()}});
}

template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
template <std::size_t R, std::array<index_t, 1> DstLower>
  requires(R != 1)
Array<T, Rank, Lower>::operator ArrayRef<const T, 1, DstLower>()
    const noexcept {
  return ArrayRef<const T, 1, DstLower>(
      data(), std::array<index_t, 1>{{size()}});
}

template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
ArrayRef<T, 1> Array<T, Rank, Lower>::section(index_t lo, index_t hi,
                                              index_t stride) noexcept {
  static_assert(Rank == 1, "section(lo,hi,stride) is rank-1 only");
  return ArrayRef<T, Rank>(static_cast<ArrayRef<T, Rank>>(*this))
      .section(lo, hi, stride);
}

template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
template <typename... Subs>
  requires(... || detail::is_slice_v<Subs>)
auto Array<T, Rank, Lower>::section(Subs... subs) noexcept {
  return ArrayRef<T, Rank>(static_cast<ArrayRef<T, Rank>>(*this))
      .section(subs...);
}

template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
ArrayRef<const T, 1> Array<T, Rank, Lower>::section(
    index_t lo, index_t hi, index_t stride) const noexcept {
  static_assert(Rank == 1, "section(lo,hi,stride) is rank-1 only");
  return ArrayRef<const T, Rank>(static_cast<ArrayRef<const T, Rank>>(*this))
      .section(lo, hi, stride);
}

template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
template <typename... Subs>
  requires(... || detail::is_slice_v<Subs>)
auto Array<T, Rank, Lower>::section(Subs... subs) const noexcept {
  return ArrayRef<const T, Rank>(static_cast<ArrayRef<const T, Rank>>(*this))
      .section(subs...);
}

// ---- List-directed array output -------------------------------------------

namespace detail {
template <typename OS, typename T> void stream_element(OS &os, const T &v) {
  if constexpr (std::is_same_v<std::remove_cv_t<T>, bool>) {
    os << (v ? 'T' : 'F');  // Fortran logical output
  } else {
    os << v;
  }
}
} // namespace detail

/// Print an owning array's elements in Fortran (column-major) order,
/// space-separated, for list-directed ``print *`` of a whole array.
template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
std::ostream &operator<<(std::ostream &os, const Array<T, Rank, Lower> &a) {
  bool first = true;
  a.for_each([&](const T &v) {
    if (!first) {
      os << ' ';
    }
    detail::stream_element(os, v);
    first = false;
  });
  return os;
}

/// Same for a non-owning view (whole-array or section).
template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
std::ostream &operator<<(std::ostream &os,
                         const ArrayRef<T, Rank, Lower> &a) {
  bool first = true;
  a.for_each([&](const T &v) {
    if (!first) {
      os << ' ';
    }
    detail::stream_element(os, v);
    first = false;
  });
  return os;
}

// ---- List-directed array input --------------------------------------------

/// Read a whole array's elements (Fortran column-major order) — Fortran
/// list-directed ``read`` of an array variable / section.
template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
std::istream &operator>>(std::istream &is, Array<T, Rank, Lower> &a) {
  const index_t n = a.size();
  for (index_t i = 0; i < n; ++i) {
    is >> a.linear_at(i);
  }
  return is;
}

template <typename T, std::size_t Rank, std::array<index_t, Rank> Lower>
std::istream &operator>>(std::istream &is,
                         const ArrayRef<T, Rank, Lower> &a) {
  const index_t n = a.size();
  for (index_t i = 0; i < n; ++i) {
    is >> a.linear_at(i);
  }
  return is;
}

// ---- ASSOCIATED ----------------------------------------------------------

/// ASSOCIATED for a scalar pointer (``T*``).
template <typename T> bool associated(T *p) noexcept { return p != nullptr; }

/// ASSOCIATED for an array pointer (non-owning ArrayRef view).
template <typename T, std::size_t R>
bool associated(const ArrayRef<T, R> &p) noexcept {
  return p.data() != nullptr;
}

/// Fortran storage association of a scalar actual to a dummy of a
/// *different* arithmetic type passed under an implicit interface
/// (``DOUBLE PRECISION`` actual, ``INTEGER`` dummy, and the like).  The
/// dummy aliases the actual's storage rather than taking a converted
/// value, so view the lvalue's bytes as the dummy's type.
template <typename To, typename From>
To &storage_ref(From &x) noexcept {
  return *reinterpret_cast<To *>(&x);
}

/// Fortran storage association of a whole-array actual to a rank-1 dummy of
/// a *different* element type (e.g. a ``DOUBLE PRECISION`` array passed to
/// an ``INTEGER`` copy routine).  Reinterpret the contiguous storage as a
/// flat view of the dummy's element type, rescaling the element count by
/// the size ratio.
template <typename To, typename Src>
ArrayRef<To, 1> reinterpret_array(const Src &v) noexcept {
  using From = std::remove_reference_t<decltype(*v.data())>;
  auto bytes = static_cast<index_t>(v.size()) * static_cast<index_t>(sizeof(From));
  auto base = reinterpret_cast<To *>(
      const_cast<std::remove_const_t<From> *>(v.data()));
  return ArrayRef<To, 1>(base, {bytes / static_cast<index_t>(sizeof(To))});
}

/// Fortran sequence association of an array *element* actual to an array
/// dummy: ``call s(a(i,j))`` where ``s``'s dummy is an array views the
/// storage from ``a(i,j)`` to the end of ``a`` (column-major).  The
/// element's address is the start; the extent is the remaining elements.
template <typename T, std::size_t R, std::array<index_t, R> Lower,
          typename... Idx>
ArrayRef<T, 1> elem_tail(Array<T, R, Lower> &a, Idx... idx) {
  T *base = &a(static_cast<index_t>(idx)...);
  return ArrayRef<T, 1>(base, {a.size() - static_cast<index_t>(base - a.data())});
}
template <typename T, std::size_t R, std::array<index_t, R> SrcLower,
          typename... Idx>
ArrayRef<T, 1> elem_tail(const ArrayRef<T, R, SrcLower> &a, Idx... idx) {
  T *base = &a(static_cast<index_t>(idx)...);
  // If ``a`` is itself an assumed-size view (a scalar sequence-associated
  // with an array dummy, or the tail of one), its real extent is unknown --
  // ``size()`` reports the sentinel-as-1 (see ``total_size``).  Computing
  // ``size() - offset`` would collapse the element tail to a bogus count
  // (SPICE's EK write path: ``zzekue04``'s scalar ``IVALS`` -> ``zzekad04``
  // assumed-size ``IVALS(*)`` -> ``dasudi`` -> ``dasuri``, which then reads
  // ``DATA(2)`` from a size-1 view).  Keep the tail assumed-size so the
  // callee indexes into the caller's real storage.
  for (index_t e : a.extents()) {
    if (e == detail::kAssumedExtent) {
      return ArrayRef<T, 1>(base, {detail::kAssumedExtent});
    }
  }
  return ArrayRef<T, 1>(base, {a.size() - static_cast<index_t>(base - a.data())});
}

/// Sequence-association view of an array element with the dummy's
/// declared extent.  When a Fortran call site passes ``A(I)`` to a
/// dummy declared ``DIMENSION X(N)``, the dummy sees N elements
/// starting at ``&A(I)`` -- not the remainder of ``A``.  NRLMSISE-00's
/// driver passes ``AP(I)`` to GTD7's ``DIMENSION AP(7)``; the bounded
/// view must report size 7 (matching the callee's declaration) so the
/// callee's internal indexing into ap(1..7) is in range, even when
/// ``A`` has only ``I-1+7`` elements.  Underlying storage validity is
/// the caller's responsibility (same as Fortran's sequence assoc).
template <typename T, std::size_t R, std::array<index_t, R> Lower,
          typename... Idx>
ArrayRef<T, 1> elem_tail_n(Array<T, R, Lower> &a, index_t n, Idx... idx) {
  T *base = &a(static_cast<index_t>(idx)...);
  return ArrayRef<T, 1>(base, {n});
}
template <typename T, std::size_t R, std::array<index_t, R> SrcLower,
          typename... Idx>
ArrayRef<T, 1> elem_tail_n(const ArrayRef<T, R, SrcLower> &a, index_t n,
                           Idx... idx) {
  T *base = &a(static_cast<index_t>(idx)...);
  return ArrayRef<T, 1>(base, {n});
}

/// Normalize an assumed-size (``A(*)`` / ``A(M,*)``) dummy at callee entry.
/// Fortran places no upper-bound check on an assumed-size array's last
/// dimension -- the callee may index as far as the actual's real storage
/// extends, which is the caller's responsibility.  The received view,
/// however, carries whatever concrete extent the actual happened to have,
/// which can be smaller than the callee legitimately accesses (e.g. a value
/// buffer forwarded down a chain of ``(*)`` dummies).  Give the last
/// dimension an unbounded (``kAssumedExtent``) extent so a valid index does
/// not trip a debug bounds check.  Leading extents and strides are
/// unchanged, so column-major offsets stay correct.
template <typename T, std::size_t R, std::array<index_t, R> Lower>
ArrayRef<T, R, Lower> assume_size(const ArrayRef<T, R, Lower> &a) {
  std::array<index_t, R> extents = a.extents();
  extents[R - 1] = detail::kAssumedExtent;
  return ArrayRef<T, R, Lower>(a.data(), a.lower_bounds(), extents, a.strides());
}

/// Fortran sequence association: view a contiguous rank-1 actual as a
/// higher-rank, explicit-shape dummy.  Fortran lets a contiguous array
/// (or array section) be passed to a dummy of a different rank; the
/// storage is reinterpreted column-major with the dummy's bounds.  Used
/// at call sites where the actual's rank is below the dummy's.
template <std::size_t R, typename T>
ArrayRef<T, R> seq_assoc(const ArrayRef<T, 1, detail::runtime_lower<1>()> &flat,
                         const std::array<index_t, R> &lower,
                         const std::array<index_t, R> &extents) {
  return ArrayRef<T, R>(flat.data(), lower, extents);
}

/// Same, for a whole *owning* array actual (``Array<T,1>``): template
/// deduction won't see the Array -> ArrayRef conversion through the
/// ``ArrayRef<T,1>`` parameter, so accept the Array directly.  Generic
/// over the source array's ``Lower`` so a static-bound caller can pass
/// its array through here too.
template <std::size_t R, typename T, std::array<index_t, 1> SrcLower>
ArrayRef<T, R> seq_assoc(Array<T, 1, SrcLower> &a,
                         const std::array<index_t, R> &lower,
                         const std::array<index_t, R> &extents) {
  return ArrayRef<T, R>(a.data(), lower, extents);
}

/// Sequence association from a multi-dimensional array *element*: pass
/// ``ARR(i,j,k)`` to a higher-rank explicit-shape dummy -- the SPICE
/// ``CALL MXM(.., REF(1,1,I), ..)`` idiom where ``REF`` is ``(3,3,20)`` and
/// the dummy is ``M(3,3)``.  The element's address is the column-major
/// origin; the dummy's bounds reinterpret the storage from there.  Generic
/// over the actual (``Array`` or ``ArrayRef``) and its element type.
template <std::size_t R, typename A, typename... Idx>
auto seq_assoc_at(A &a, const std::array<index_t, R> &lower,
                  const std::array<index_t, R> &extents, Idx... idx)
    -> ArrayRef<std::remove_reference_t<decltype(a(static_cast<index_t>(
                    idx)...))>,
                R> {
  using T = std::remove_reference_t<decltype(a(static_cast<index_t>(idx)...))>;
  return ArrayRef<T, R>(&a(static_cast<index_t>(idx)...), lower, extents);
}

/// Same as :func:`seq_assoc_at` but for an *assumed-size* higher-rank dummy
/// (``A(M, *)`` -- the SPICE ``ZZELLPLT``/``ZZCAPPLT(.., PLATES(1,PIX))``
/// idiom).  The leading extents are fixed by the dummy; the trailing
/// (assumed) extent is unknown, so it spans the rest of the actual's
/// storage from the element: ``(remaining elements) / (product of the
/// leading extents)``.  The caller passes the leading extents with a
/// placeholder (any value) in the last slot, which we overwrite.  Without
/// this the element decayed through ``ArrayRef(T&)`` to a single-cell
/// ``{1,1,...}`` view and the callee's first non-trivial index overran it.
template <std::size_t R, typename A, typename... Idx>
auto seq_assoc_at_rest(A &a, const std::array<index_t, R> &lower,
                       std::array<index_t, R> extents, Idx... idx)
    -> ArrayRef<std::remove_reference_t<decltype(a(static_cast<index_t>(
                    idx)...))>,
                R> {
  using T = std::remove_reference_t<decltype(a(static_cast<index_t>(idx)...))>;
  T *base = &a(static_cast<index_t>(idx)...);
  // If ``a`` is itself assumed-size, its ``size()`` is the sentinel-as-1;
  // computing ``rest`` from it would collapse the trailing extent.  Keep
  // the tail assumed-size (mirrors the rank-1 ``elem_tail`` guard).
  for (index_t e : a.extents()) {
    if (e == detail::kAssumedExtent) {
      extents[R - 1] = detail::kAssumedExtent;
      return ArrayRef<T, R>(base, lower, extents);
    }
  }
  index_t rest = a.size() - static_cast<index_t>(base - a.data());
  index_t prod = 1;
  for (std::size_t i = 0; i + 1 < R; ++i) prod *= extents[i];
  extents[R - 1] = prod > 0 ? rest / prod : 0;
  return ArrayRef<T, R>(base, lower, extents);
}

/// Fortran sequence association of a whole-array actual to a *scalar*
/// dummy: the dummy is storage-associated with the array's first element.
/// Returns a reference to that element (column-major origin = ``data()``),
/// preserving const-ness and rank-agnostic across ``Array`` / ``ArrayRef``.
/// A character *cell* (``CharArrayRef``) has its own overload below, since
/// its ``data()`` is a raw ``char*`` and the first element is a whole cell.
class CharArrayRef;  // defined below; excluded from the generic ``first``

template <typename A>
  requires(!std::is_same_v<std::remove_cvref_t<A>, CharArrayRef>)
constexpr decltype(auto) first(A &&a) {
  return *a.data();
}

// ---- Assumed-length CHARACTER array dummy ---------------------------------

/// Non-owning view of a rank-1 array of characters whose element length
/// the *caller* fixes — the dummy form of an assumed-length array
/// ``CHARACTER*(*) X(*)`` (a SPICE "character cell").  Indexing yields a
/// CharRef, so ``x(i)`` reads or writes element ``i`` with Fortran
/// blank-pad / truncate semantics.  The element type isn't a single C++
/// type (the length is a runtime value), so this can't be a plain
/// ``ArrayRef``; it carries the base pointer, element length, lower
/// bound, and count instead.
class CharArrayRef {
public:
  /// Null view.  Arises for an assumed-length character array that is an
  /// ENTRY-shared dummy of a *sibling* entry: the duplicated body declares
  /// it as a local but the code that uses it is unreachable for this entry.
  constexpr CharArrayRef() noexcept
      : base_(nullptr), elem_len_(0), lower_(1), count_(0) {}
  constexpr CharArrayRef(char *base, std::size_t elem_len, index_t lower,
                         index_t count) noexcept
      : base_(base), elem_len_(elem_len), lower_(lower), count_(count) {}

  /// From a fixed-length character array actual (contiguous FortranString
  /// elements, each exactly ``N`` bytes).
  template <std::size_t N>
  CharArrayRef(Array<FortranString<N>, 1> &a) noexcept
      : base_(reinterpret_cast<char *>(a.data())), elem_len_(N),
        lower_(a.lbound(1)), count_(a.size()) {}
  /// From a single character scalar (scalar/array storage association).
  template <std::size_t N>
  CharArrayRef(FortranString<N> &s) noexcept
      : base_(s.data()), elem_len_(N), lower_(1), count_(1) {}
  /// From a fixed-length character-array *view* (a char array forwarded
  /// from one dummy to another).  Any source lower bound (a static-lb
  /// ``ARRAY(*)`` dummy view included).
  template <std::size_t N, std::array<index_t, 1> SrcLower>
  CharArrayRef(ArrayRef<FortranString<N>, 1, SrcLower> a) noexcept
      : base_(reinterpret_cast<char *>(a.data())), elem_len_(N),
        lower_(a.lbound(1)), count_(a.size()) {}
  /// From a single character scalar view (scalar/array storage assoc).
  CharArrayRef(CharRef s) noexcept
      : base_(s.data()), elem_len_(s.size()), lower_(1), count_(1) {}
  /// From a character literal / value passed to a character-array dummy
  /// (``call s(cell, ..., ' ')``): a one-element cell over the (read-only)
  /// characters, valid for the duration of the call.
  CharArrayRef(std::string_view s) noexcept
      : base_(const_cast<char *>(s.data())), elem_len_(s.size()), lower_(1),
        count_(1) {}

  /// 1-based element ``x(i)`` as a writable character view.
  constexpr CharRef operator()(index_t i) const noexcept {
    return CharRef(base_ + (i - lower_) * static_cast<index_t>(elem_len_),
                   elem_len_);
  }
  /// Sequence association from element ``i`` onward (``call s(cell(i))``).
  constexpr CharArrayRef from_element(index_t i) const noexcept {
    const index_t off = i - lower_;
    return CharArrayRef(base_ + off * static_cast<index_t>(elem_len_),
                        elem_len_, 1, count_ - off);
  }
  constexpr index_t size() const noexcept { return count_; }
  constexpr index_t lbound(std::size_t = 1) const noexcept { return lower_; }
  constexpr index_t ubound(std::size_t = 1) const noexcept {
    return lower_ + count_ - 1;
  }
  constexpr char *data() const noexcept { return base_; }
  constexpr std::size_t elem_len() const noexcept { return elem_len_; }

private:
  char *base_;
  std::size_t elem_len_;
  index_t lower_;
  index_t count_;
};

/// Character-array overload of :func:`elem_tail_n`: a SPICE call site
/// passes ``CELL(I)`` to an assumed-length CHARACTER array dummy
/// ``CHARACTER*(*) DUMMY(N)``.  The dummy sees ``n`` elements starting at
/// element ``i`` of the caller's ``CharArrayRef``, with the caller's
/// element length carried through.
inline CharArrayRef elem_tail_n(CharArrayRef &a, index_t n, index_t i) {
  const index_t off = i - a.lbound();
  return CharArrayRef(a.data() + off * static_cast<index_t>(a.elem_len()),
                      a.elem_len(), 1, n);
}

/// Sequence association of a character-cell element actual to a character
/// array dummy: ``call s(cell(i))`` views the cell from element ``i`` on.
inline CharArrayRef elem_tail(const CharArrayRef &cell, index_t i) noexcept {
  return cell.from_element(i);
}

/// Whole character-array actual passed to a *scalar* character dummy: the
/// dummy is storage-associated with the array's first cell.  Overrides the
/// generic ``first`` (whose ``*data()`` would yield a single ``char``) so
/// the result is a ``CharRef`` over the whole first element.
inline CharRef first(const CharArrayRef &cell) noexcept {
  return cell(cell.lbound(1));
}

} // namespace ftn

// std::format support: a whole array formats its elements (Fortran
// element / column-major order) back-to-back, each with the element
// format spec — matching ``write(u,'(2i4)') name`` style output.
namespace ftn::detail {
template <typename Arr, typename T>
struct array_formatter : std::formatter<T, char> {
  template <typename FmtContext>
  auto format(const Arr &a, FmtContext &ctx) const {
    for (index_t i = 0; i < a.size(); ++i) {
      ctx.advance_to(std::formatter<T, char>::format(a.linear_at(i), ctx));
    }
    return ctx.out();
  }
};
} // namespace ftn::detail

template <typename T, std::size_t R, std::array<ftn::index_t, R> Lower>
struct std::formatter<ftn::Array<T, R, Lower>, char>
    : ftn::detail::array_formatter<ftn::Array<T, R, Lower>, T> {};

template <typename T, std::size_t R>
struct std::formatter<ftn::ArrayRef<T, R>, char>
    : ftn::detail::array_formatter<ftn::ArrayRef<T, R>,
                                       std::remove_cv_t<T>> {};

#endif // FORTRAN_RT_ARRAY_REF_HPP

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
#include "string.hpp"

#include <array>
#include <cassert>
#include <cstddef>
#include <format>
#include <istream>
#include <ostream>
#include <tuple>
#include <type_traits>
#include <utility>

namespace fortran {

template <typename T, std::size_t Rank> class ArrayRef {
  static_assert(Rank >= 1, "fortran::ArrayRef rank must be >= 1");

public:
  using element_type = T;
  using value_type = std::remove_cv_t<T>;
  using extent_array = std::array<index_t, Rank>;
  using lower_array = std::array<index_t, Rank>;
  static constexpr std::size_t rank = Rank;

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

  /// Add ``const``: a mutable view converts to a read-only view of the
  /// same data (``ArrayRef<T>`` -> ``ArrayRef<const T>``), so a mutable
  /// array/section can be passed where a ``const`` view is expected.
  /// Only enabled when ``T`` is ``const U`` for the source's ``U``.
  template <typename U>
    requires(std::is_const_v<T> && std::is_same_v<std::remove_const_t<T>, U>)
  ArrayRef(const ArrayRef<U, Rank> &other) noexcept
      : data_(other.data()), lower_(other.lower_bounds()),
        extents_(other.extents()), strides_(other.strides()) {}

  /// Fortran storage association: a scalar actual passed to an
  /// (assumed-size) array dummy is that dummy's sole element — a view of
  /// one element, every extent 1.
  ArrayRef(T &scalar) noexcept
      : ArrayRef(&scalar, lower_array_filled(1)) {}

  /// Same, for a ``const`` or rvalue scalar actual (an intent(in) value,
  /// a literal, or an expression) — read in the callee, valid for the
  /// call.  Disabled when the element is already ``const`` (the overload
  /// above covers it) to avoid a redeclaration.
  template <typename U = T>
    requires(!std::is_const_v<U>)
  ArrayRef(const T &scalar) noexcept
      : ArrayRef(const_cast<T *>(&scalar), lower_array_filled(1)) {}

  /// Sequence association from a higher-rank view: flatten to a 1-D view
  /// over the contiguous storage (extent = total element count).  Rank-1
  /// target only; mirrors the Array<T,Rank> -> ArrayRef<T,1> conversion.
  template <std::size_t R2>
    requires(Rank == 1 && R2 != 1)
  ArrayRef(const ArrayRef<T, R2> &other) noexcept
      : ArrayRef(other.data(), extent_array{{other.size()}}) {}

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

// Sequence association: flatten a higher-rank array to a rank-1 view over
// its contiguous storage (extent = total element count, lower bound 1).
template <typename T, std::size_t Rank>
template <std::size_t R>
  requires(R != 1)
Array<T, Rank>::operator ArrayRef<T, 1>() noexcept {
  return ArrayRef<T, 1>(data(), std::array<index_t, 1>{{size()}});
}

template <typename T, std::size_t Rank>
template <std::size_t R>
  requires(R != 1)
Array<T, Rank>::operator ArrayRef<const T, 1>() const noexcept {
  return ArrayRef<const T, 1>(data(), std::array<index_t, 1>{{size()}});
}

template <typename T, std::size_t Rank>
ArrayRef<T, 1> Array<T, Rank>::section(index_t lo, index_t hi,
                                       index_t stride) noexcept {
  static_assert(Rank == 1, "section(lo,hi,stride) is rank-1 only");
  return ArrayRef<T, Rank>(*this).section(lo, hi, stride);
}

template <typename T, std::size_t Rank>
template <typename... Subs>
  requires(... || detail::is_slice_v<Subs>)
auto Array<T, Rank>::section(Subs... subs) noexcept {
  return ArrayRef<T, Rank>(*this).section(subs...);
}

template <typename T, std::size_t Rank>
ArrayRef<const T, 1> Array<T, Rank>::section(index_t lo, index_t hi,
                                             index_t stride) const noexcept {
  static_assert(Rank == 1, "section(lo,hi,stride) is rank-1 only");
  return ArrayRef<const T, Rank>(*this).section(lo, hi, stride);
}

template <typename T, std::size_t Rank>
template <typename... Subs>
  requires(... || detail::is_slice_v<Subs>)
auto Array<T, Rank>::section(Subs... subs) const noexcept {
  return ArrayRef<const T, Rank>(*this).section(subs...);
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
template <typename T, std::size_t Rank>
std::ostream &operator<<(std::ostream &os, const Array<T, Rank> &a) {
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
template <typename T, std::size_t Rank>
std::ostream &operator<<(std::ostream &os, const ArrayRef<T, Rank> &a) {
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
template <typename T, std::size_t Rank>
std::istream &operator>>(std::istream &is, Array<T, Rank> &a) {
  const index_t n = a.size();
  for (index_t i = 0; i < n; ++i) {
    is >> a.linear_at(i);
  }
  return is;
}

template <typename T, std::size_t Rank>
std::istream &operator>>(std::istream &is, const ArrayRef<T, Rank> &a) {
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

/// Fortran sequence association of an array *element* actual to an array
/// dummy: ``call s(a(i,j))`` where ``s``'s dummy is an array views the
/// storage from ``a(i,j)`` to the end of ``a`` (column-major).  The
/// element's address is the start; the extent is the remaining elements.
template <typename T, std::size_t R, typename... Idx>
ArrayRef<T, 1> elem_tail(Array<T, R> &a, Idx... idx) {
  T *base = &a(static_cast<index_t>(idx)...);
  return ArrayRef<T, 1>(base, {a.size() - static_cast<index_t>(base - a.data())});
}
template <typename T, std::size_t R, typename... Idx>
ArrayRef<T, 1> elem_tail(const ArrayRef<T, R> &a, Idx... idx) {
  T *base = &a(static_cast<index_t>(idx)...);
  return ArrayRef<T, 1>(base, {a.size() - static_cast<index_t>(base - a.data())});
}

/// Fortran sequence association: view a contiguous rank-1 actual as a
/// higher-rank, explicit-shape dummy.  Fortran lets a contiguous array
/// (or array section) be passed to a dummy of a different rank; the
/// storage is reinterpreted column-major with the dummy's bounds.  Used
/// at call sites where the actual's rank is below the dummy's.
template <std::size_t R, typename T>
ArrayRef<T, R> seq_assoc(const ArrayRef<T, 1> &flat,
                         const std::array<index_t, R> &lower,
                         const std::array<index_t, R> &extents) {
  return ArrayRef<T, R>(flat.data(), lower, extents);
}

/// Fortran sequence association of a whole-array actual to a *scalar*
/// dummy: the dummy is storage-associated with the array's first element.
/// Returns a reference to that element (column-major origin = ``data()``),
/// preserving const-ness and rank-agnostic across ``Array`` / ``ArrayRef``.
template <typename A>
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
  /// from one dummy to another).
  template <std::size_t N>
  CharArrayRef(ArrayRef<FortranString<N>, 1> a) noexcept
      : base_(reinterpret_cast<char *>(a.data())), elem_len_(N),
        lower_(a.lbound(1)), count_(a.size()) {}
  /// From a single character scalar view (scalar/array storage assoc).
  CharArrayRef(CharRef s) noexcept
      : base_(s.data()), elem_len_(s.size()), lower_(1), count_(1) {}

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

private:
  char *base_;
  std::size_t elem_len_;
  index_t lower_;
  index_t count_;
};

/// Sequence association of a character-cell element actual to a character
/// array dummy: ``call s(cell(i))`` views the cell from element ``i`` on.
inline CharArrayRef elem_tail(const CharArrayRef &cell, index_t i) noexcept {
  return cell.from_element(i);
}

} // namespace fortran

// std::format support: a whole array formats its elements (Fortran
// element / column-major order) back-to-back, each with the element
// format spec — matching ``write(u,'(2i4)') name`` style output.
namespace fortran::detail {
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
} // namespace fortran::detail

template <typename T, std::size_t R>
struct std::formatter<fortran::Array<T, R>, char>
    : fortran::detail::array_formatter<fortran::Array<T, R>, T> {};

template <typename T, std::size_t R>
struct std::formatter<fortran::ArrayRef<T, R>, char>
    : fortran::detail::array_formatter<fortran::ArrayRef<T, R>,
                                       std::remove_cv_t<T>> {};

#endif // FORTRAN_RT_ARRAY_REF_HPP

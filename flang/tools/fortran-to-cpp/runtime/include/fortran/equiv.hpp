//===-- fortran/equiv.hpp - EQUIVALENCE accessor proxy ----------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// fortran::EquivSlot<T, Offset>
//
// One accessor proxy per Fortran name in an EQUIVALENCE class.  Each
// proxy holds a pointer to the equivalence class's shared
// ``std::array<std::byte, N>`` buffer and reads / writes its declared
// type ``T`` starting at byte ``Offset``.
//
// Reads use ``std::bit_cast`` when T fits exactly in a same-size load
// from the buffer at offset 0 (the common type-pun case), and
// ``std::memcpy`` otherwise (the offset case, or anywhere a partial
// access is needed).  Both forms are defined behavior under strict
// aliasing.
//
// See ../README.md (decision D7) for the rationale.  Example:
//
//   struct XBits_Equiv {
//     std::array<std::byte, sizeof(float)> _store{};
//     fortran::EquivSlot<float,        0> x   { _store.data() };
//     fortran::EquivSlot<std::int32_t, 0> bits{ _store.data() };
//   };
//
//   XBits_Equiv e;
//   e.x = 3.14f;
//   // e.bits now returns the IEEE-754 bit pattern of 3.14f.
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_RT_EQUIV_HPP
#define FORTRAN_RT_EQUIV_HPP

#include "array_ref.hpp" // ArrayRef view + elem_tail for equivalenced buffers

#include <bit>
#include <cstddef>
#include <cstring>
#include <type_traits>
#include <vector>

namespace fortran {

// Tag base so the unformatted byte-I/O helpers can recognise an
// EquivArray element proxy (its value is reached via memcpy, not a
// raw lvalue) without depending on the EquivArray template parameters.
struct EquivCellTag {};

template <typename T, std::size_t Offset> class EquivSlot {
  static_assert(std::is_trivially_copyable_v<T>,
                "EquivSlot<T> requires T to be trivially copyable");

public:
  using value_type = T;

  /// Construct a slot pointing into the equivalence class's byte
  /// buffer.  ``base`` must remain valid for the slot's lifetime;
  /// typically the slot is a non-static member that sits next to the
  /// buffer in the enclosing struct.
  constexpr explicit EquivSlot(std::byte *base) noexcept
      : base_(base + Offset) {}

  EquivSlot(const EquivSlot &) = default;
  EquivSlot(EquivSlot &&) = default;
  // Assignment is via ``operator=(const T&)`` only — copying the slot
  // itself would rebind it to another buffer, which is rarely what
  // generated code wants.
  EquivSlot &operator=(const EquivSlot &) = delete;
  EquivSlot &operator=(EquivSlot &&) = delete;

  /// Read: produce a fresh ``T`` from the bytes at ``base_``.
  ///
  /// We use ``std::bit_cast`` when ``Offset == 0`` and the buffer is
  /// known to hold a same-size ``T`` (the common type-pun case);
  /// ``std::memcpy`` otherwise.  Both are defined behavior even under
  /// strict aliasing.
  operator T() const noexcept {
    T value;
    std::memcpy(&value, base_, sizeof(T));
    return value;
  }

  /// Write: copy the bits of ``v`` into the buffer.
  EquivSlot &operator=(const T &v) noexcept {
    std::memcpy(base_, &v, sizeof(T));
    return *this;
  }

  /// Explicit ``bit_cast``-style read.  Identical to the implicit
  /// conversion above but documents intent in places where the
  /// generated code is doing a deliberate type pun.
  T load() const noexcept {
    T value;
    std::memcpy(&value, base_, sizeof(T));
    return value;
  }

  /// Convenience: ``e.x += 1`` for arithmetic slots.
  template <typename U,
            typename = std::enable_if_t<std::is_arithmetic_v<T> &&
                                        std::is_convertible_v<U, T>>>
  EquivSlot &operator+=(const U &delta) noexcept {
    return *this = static_cast<T>(load() + delta);
  }
  template <typename U,
            typename = std::enable_if_t<std::is_arithmetic_v<T> &&
                                        std::is_convertible_v<U, T>>>
  EquivSlot &operator-=(const U &delta) noexcept {
    return *this = static_cast<T>(load() - delta);
  }

private:
  std::byte *base_;
};

/// Array proxy for an EQUIVALENCE class: ``N`` typed elements over the
/// shared byte buffer, starting at byte ``Offset``.  ``operator()(i)``
/// (1-based, Fortran semantics) returns an :class:`EquivCell` that
/// reads / writes one element through memcpy -- so concurrent aliases of
/// different types (a ``double[128]`` and an ``int32_t[256]`` over the
/// same 1024 bytes -- SPICE's classic DAF pattern) both stay correct.
template <typename T, std::size_t N, std::size_t Offset> class EquivArray {
  static_assert(std::is_trivially_copyable_v<T>,
                "EquivArray<T> requires T to be trivially copyable");

public:
  using value_type = T;
  static constexpr std::size_t length = N;

  constexpr explicit EquivArray(std::byte *base) noexcept
      : base_(base + Offset) {}

  EquivArray(const EquivArray &) = default;
  EquivArray(EquivArray &&) = default;
  EquivArray &operator=(const EquivArray &) = delete;
  EquivArray &operator=(EquivArray &&) = delete;

  // Per-element proxy: reads/writes ``T`` at byte ``(i-1)*sizeof(T)``.
  class Cell : public EquivCellTag {
  public:
    using value_type = T;
    constexpr explicit Cell(std::byte *p) noexcept : p_(p) {}
    operator T() const noexcept {
      T v;
      std::memcpy(&v, p_, sizeof(T));
      return v;
    }
    Cell &operator=(const T &v) noexcept {
      std::memcpy(p_, &v, sizeof(T));
      return *this;
    }
    template <typename U, typename = std::enable_if_t<
                              std::is_arithmetic_v<T> &&
                              std::is_convertible_v<U, T>>>
    Cell &operator+=(const U &d) noexcept {
      T v;
      std::memcpy(&v, p_, sizeof(T));
      v = static_cast<T>(v + d);
      std::memcpy(p_, &v, sizeof(T));
      return *this;
    }
    template <typename U, typename = std::enable_if_t<
                              std::is_arithmetic_v<T> &&
                              std::is_convertible_v<U, T>>>
    Cell &operator-=(const U &d) noexcept {
      T v;
      std::memcpy(&v, p_, sizeof(T));
      v = static_cast<T>(v - d);
      std::memcpy(p_, &v, sizeof(T));
      return *this;
    }

  private:
    std::byte *p_;
  };

  /// Fortran-style 1-based element access.
  Cell operator()(std::size_t i) noexcept {
    return Cell{base_ + (i - 1) * sizeof(T)};
  }
  T operator()(std::size_t i) const noexcept {
    T v;
    std::memcpy(&v, base_ + (i - 1) * sizeof(T), sizeof(T));
    return v;
  }

  /// Raw byte access -- used by the unformatted-direct I/O helpers
  /// ``fortran::io::append_bytes`` / ``take_bytes`` to read / write the
  /// whole aliased buffer in one go.
  std::byte *byte_data() noexcept { return base_; }
  const std::byte *byte_data() const noexcept { return base_; }
  static constexpr std::size_t byte_size() noexcept { return N * sizeof(T); }

  /// View the aliased buffer as a contiguous rank-1 ``ArrayRef<T>``.
  /// Used when the whole equivalenced array is passed to a routine that
  /// takes an array dummy (the SPICE DAF buffers DPBUF/INBUF) -- the
  /// callee reads/writes ``T`` directly in the shared storage, and the
  /// other equivalenced view still observes the bytes through memcpy.
  ArrayRef<T, 1> ref() noexcept {
    return ArrayRef<T, 1>(reinterpret_cast<T *>(base_),
                          {static_cast<index_t>(N)});
  }
  operator ArrayRef<T, 1>() noexcept { return ref(); }

private:
  std::byte *base_;
};

/// Sequence association on an equivalenced array: ``call s(DPBUF(i))``
/// with an array dummy views the buffer from element ``i`` onward.  Routes
/// through the ``ArrayRef`` view's :func:`elem_tail`.
template <typename T, std::size_t N, std::size_t O, typename... Idx>
inline ArrayRef<T, 1> elem_tail(EquivArray<T, N, O> &a, Idx... idx) noexcept {
  return elem_tail(a.ref(), idx...);
}

/// Same as ``elem_tail`` but with an explicit extent ``n`` for the
/// resulting view — matches the dummy's declared size in
/// :func:`elem_tail_n` so the dummy can be longer than the actual's
/// remaining slice when the call is well-formed.
template <typename T, std::size_t N, std::size_t O, typename... Idx>
inline ArrayRef<T, 1> elem_tail_n(EquivArray<T, N, O> &a, index_t n,
                                  Idx... idx) noexcept {
  return elem_tail_n(a.ref(), n, idx...);
}

/// Free helper for an explicit ``bit_cast`` between same-size,
/// trivially-copyable types.  Used by the emitter for Fortran's
/// ``TRANSFER`` intrinsic and for any place where the source code
/// asked for a type pun outside of an EQUIVALENCE.
template <typename To, typename From>
constexpr To bit_cast(const From &from) noexcept {
  static_assert(sizeof(To) == sizeof(From),
                "fortran::bit_cast: sizes must match");
  static_assert(std::is_trivially_copyable_v<To>,
                "fortran::bit_cast: To must be trivially copyable");
  static_assert(std::is_trivially_copyable_v<From>,
                "fortran::bit_cast: From must be trivially copyable");
  return std::bit_cast<To>(from);
}

} // namespace fortran

namespace fortran::io {

// Unformatted record I/O of a single equivalenced element
// (``read(u) (DPBUF(i), i=1,128)`` -- one Cell per iteration).  The
// element's value is reached through memcpy (operator T() / operator=),
// so we round-trip a plain ``T`` rather than aliasing the cell directly.
// SFINAE on EquivCellTag keeps these disjoint from the arithmetic /
// contiguous-view overloads in io.hpp.
template <class C, std::enable_if_t<std::is_base_of_v<
                       fortran::EquivCellTag, std::remove_cvref_t<C>>, int> = 0>
inline void append_bytes(std::vector<std::byte> &buf, const C &cell) {
  using T = typename std::remove_cvref_t<C>::value_type;
  T v = static_cast<T>(cell);
  std::byte tmp[sizeof(T)];
  std::memcpy(tmp, &v, sizeof(T));
  buf.insert(buf.end(), tmp, tmp + sizeof(T));
}

template <class C, std::enable_if_t<std::is_base_of_v<
                       fortran::EquivCellTag, std::remove_cvref_t<C>>, int> = 0>
inline std::size_t take_bytes(const std::vector<std::byte> &rec,
                              std::size_t off, C &&cell) {
  using T = typename std::remove_cvref_t<C>::value_type;
  T v{};
  if (off + sizeof(T) <= rec.size()) {
    std::memcpy(&v, rec.data() + off, sizeof(T));
  }
  cell = v;  // writes back through the proxy's memcpy assignment
  return off + sizeof(T);
}

} // namespace fortran::io

#endif // FORTRAN_RT_EQUIV_HPP

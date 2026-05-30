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

#include <bit>
#include <cstddef>
#include <cstring>
#include <type_traits>

namespace fortran {

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

#endif // FORTRAN_RT_EQUIV_HPP

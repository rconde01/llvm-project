//===-- fortran/string.hpp - Fortran CHARACTER variable ---------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// fortran::FortranString<N>
//
// Fixed-length character variable with Fortran's semantics:
//   * Length N is part of the type — different N is a different type.
//   * Assignment from a shorter string pads with blanks on the right.
//   * Assignment from a longer string truncates to N characters.
//   * Equality is length-padded: 'hi' == 'hi   ' is true.
//   * Substring access (operator() for an inclusive 1..N range) returns
//     a writable proxy that performs blank-padded writes back into the
//     parent.
//
// See ../README.md (rule R5 and decision D3) for the rationale, and the
// hybrid policy:
//   * std::string_view  — character literals, intent(in) parameters.
//   * FortranString<N>  — declared fixed-length variables.
//   * std::string       — character(len=:), allocatable cases.
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_RT_STRING_HPP
#define FORTRAN_RT_STRING_HPP

#include <algorithm>
#include <array>
#include <cassert>
#include <cstddef>
#include <cstring>
#include <iosfwd>
#include <format>
#include <istream>
#include <ostream>
#include <string>
#include <string_view>

namespace fortran {

namespace detail {

/// Trim trailing blank padding for comparisons.
constexpr std::string_view rstrip_blanks(std::string_view s) noexcept {
  while (!s.empty() && s.back() == ' ') {
    s.remove_suffix(1);
  }
  return s;
}

/// Fortran-style length-padded comparison.  The shorter operand is
/// conceptually padded with blanks to match the longer one, then a
/// byte-wise compare runs.  Equivalent to comparing the rstripped
/// versions when both inputs use blank padding.
constexpr int compare_padded(std::string_view a,
                             std::string_view b) noexcept {
  const std::size_t n = std::max(a.size(), b.size());
  for (std::size_t i = 0; i < n; ++i) {
    const char ca = i < a.size() ? a[i] : ' ';
    const char cb = i < b.size() ? b[i] : ' ';
    if (ca < cb) return -1;
    if (ca > cb) return 1;
  }
  return 0;
}

} // namespace detail

class CharRef;  // a substring proxy converts to this (defined below)

template <std::size_t N> class FortranString {
  static_assert(N >= 1, "FortranString length must be >= 1");

public:
  static constexpr std::size_t length = N;

  // Default constructor: filled with blanks, matching Fortran's
  // default initial value for character variables under
  // ``-finit-character=32`` and standard expectations.
  constexpr FortranString() noexcept { data_.fill(' '); }

  // Construct from a string view with blank-pad-or-truncate semantics.
  // This is the implicit conversion path generated code relies on:
  //   FortranString<10> name = "hi";   // -> "hi        "
  constexpr FortranString(std::string_view s) noexcept { assign_(s); }
  constexpr FortranString(const char *s) noexcept
      : FortranString(std::string_view{s}) {}
  // From an assumed-length character view (CharRef): copy with the usual
  // pad/truncate, so a CHARACTER*(*) actual binds to a fixed-length
  // ``CHARACTER*N`` dummy (storage association to the first N characters).
  // Constrained to an *exact* CharRef (not merely convertible-to-CharRef)
  // so a std::string / literal -- convertible to both CharRef and
  // string_view -- still picks the string_view ctor unambiguously.  The
  // body instantiates only at a call site, where CharRef is complete.
  template <typename C>
    requires std::is_same_v<std::remove_cvref_t<C>, CharRef>
  constexpr FortranString(const C &r) noexcept {
    assign_(r.view());
  }

  // Element-by-element copy: same length is a trivial copy; different
  // length goes through assign_'s pad/truncate.
  template <std::size_t M>
  constexpr FortranString(const FortranString<M> &other) noexcept {
    assign_(other.view());
  }

  // Assignment with Fortran semantics (pad / truncate).
  constexpr FortranString &operator=(std::string_view s) noexcept {
    assign_(s);
    return *this;
  }
  constexpr FortranString &operator=(const char *s) noexcept {
    return *this = std::string_view{s};
  }
  // Assigning a character view (CharRef): an exact-CharRef constrained
  // overload (see the ctor above) so it beats the string_view path for a
  // CharRef without making a std::string assignment ambiguous.
  template <typename C>
    requires std::is_same_v<std::remove_cvref_t<C>, CharRef>
  constexpr FortranString &operator=(const C &r) noexcept {
    assign_(r.view());
    return *this;
  }
  template <std::size_t M>
  constexpr FortranString &operator=(const FortranString<M> &other) noexcept {
    assign_(other.view());
    return *this;
  }

  // ---- Views & raw data ------------------------------------------------

  /// Whole-string view — *includes* any trailing padding.  Use this
  /// when you need the declared fixed length (e.g. passing to a
  /// fixed-width routine).
  constexpr std::string_view view() const noexcept {
    return std::string_view{data_.data(), N};
  }
  constexpr operator std::string_view() const noexcept { return view(); }

  /// View without trailing blanks.  Fortran's ``TRIM`` intrinsic.
  constexpr std::string_view trimmed() const noexcept {
    return detail::rstrip_blanks(view());
  }

  /// Fortran's ``LEN_TRIM`` — length of the string after removing
  /// trailing blanks.
  constexpr std::size_t len_trim() const noexcept { return trimmed().size(); }

  /// Raw mutable / const pointers for low-level interop.
  constexpr char *data() noexcept { return data_.data(); }
  constexpr const char *data() const noexcept { return data_.data(); }

  /// Element access in Fortran's 1-based indexing.
  constexpr char &operator[](std::size_t one_based_index) noexcept {
    assert(one_based_index >= 1 && one_based_index <= N);
    return data_[one_based_index - 1];
  }
  constexpr char operator[](std::size_t one_based_index) const noexcept {
    assert(one_based_index >= 1 && one_based_index <= N);
    return data_[one_based_index - 1];
  }

  // ---- Substring proxy --------------------------------------------------

  /// Read-only proxy returned by the const ``operator()(lo, hi)``.
  /// Yields a non-owning view of the inclusive 1-based subrange.
  class ConstSubstring {
  public:
    constexpr ConstSubstring(const char *base, std::size_t size) noexcept
        : base_(base), size_(size) {}
    constexpr operator std::string_view() const noexcept {
      return std::string_view{base_, size_};
    }
    constexpr std::string_view view() const noexcept {
      return std::string_view{base_, size_};
    }
    constexpr std::size_t size() const noexcept { return size_; }
    /// Pass a substring as an assumed-length CHARACTER actual (-> CharRef).
    operator CharRef() const noexcept;

    friend std::ostream &operator<<(std::ostream &os, const ConstSubstring &s) {
      return os << s.view();
    }

  private:
    const char *base_;
    std::size_t size_;
  };

  /// Writable proxy returned by the non-const ``operator()(lo, hi)``.
  /// Assigning to it performs Fortran's substring-assignment semantics
  /// (pad with blanks if the RHS is shorter than the slice; truncate
  /// if longer).
  class Substring {
  public:
    constexpr Substring(char *base, std::size_t size) noexcept
        : base_(base), size_(size) {}

    constexpr Substring &operator=(std::string_view s) noexcept {
      const std::size_t take = std::min(s.size(), size_);
      std::copy_n(s.data(), take, base_);
      // Pad the remainder with blanks.
      for (std::size_t i = take; i < size_; ++i) {
        base_[i] = ' ';
      }
      return *this;
    }
    constexpr Substring &operator=(const char *s) noexcept {
      return *this = std::string_view{s};
    }

    constexpr operator std::string_view() const noexcept {
      return std::string_view{base_, size_};
    }
    constexpr std::string_view view() const noexcept {
      return std::string_view{base_, size_};
    }
    constexpr std::size_t size() const noexcept { return size_; }
    /// Pass a substring as an assumed-length CHARACTER actual (-> CharRef).
    operator CharRef() const noexcept;

    friend std::ostream &operator<<(std::ostream &os, const Substring &s) {
      return os << s.view();
    }
    /// List-directed read into a substring: take the next token (the
    /// substring assignment pads/truncates to the slice width).
    friend std::istream &operator>>(std::istream &is, Substring s) {
      std::string token;
      is >> token;
      s = std::string_view{token};
      return is;
    }
    /// Fortran character comparison (blank-padded), so a substring compares
    /// against another substring, a FortranString, or a literal.  An exact
    /// (Substring, Substring) overload plus a constrained template (the same
    /// shape as CharRef's) avoids the C++20 reversed-candidate ambiguity
    /// that a single string_view overload would create.  C++20 synthesizes
    /// ``!=`` from these.
    friend bool operator==(const Substring &a, const Substring &b) noexcept {
      return detail::compare_padded(a.view(), b.view()) == 0;
    }
    template <typename S>
      requires(std::is_convertible_v<const S &, std::string_view> &&
               !std::is_same_v<std::remove_cvref_t<S>, Substring>)
    friend bool operator==(const Substring &a, const S &b) noexcept {
      return detail::compare_padded(a.view(), std::string_view(b)) == 0;
    }

  private:
    char *base_;
    std::size_t size_;
  };

  /// ``name(lo, hi)``  — 1-based inclusive substring slice, matching
  /// Fortran's ``name(lo:hi)`` syntax (the colon becomes a comma in
  /// the C++ surface).  Used as an lvalue to *assign* into the slice,
  /// or as an rvalue to view it.
  constexpr Substring operator()(std::size_t lo, std::size_t hi) noexcept {
    assert(lo >= 1 && hi <= N && lo <= hi + 1);
    return Substring{data_.data() + (lo - 1), hi - lo + 1};
  }
  constexpr ConstSubstring operator()(std::size_t lo,
                                      std::size_t hi) const noexcept {
    assert(lo >= 1 && hi <= N && lo <= hi + 1);
    return ConstSubstring{data_.data() + (lo - 1), hi - lo + 1};
  }

  // ---- Comparison (Fortran's length-padded semantics) ------------------

  template <std::size_t M>
  constexpr bool operator==(const FortranString<M> &rhs) const noexcept {
    return detail::compare_padded(view(), rhs.view()) == 0;
  }
  constexpr bool operator==(std::string_view rhs) const noexcept {
    return detail::compare_padded(view(), rhs) == 0;
  }
  constexpr bool operator==(const char *rhs) const noexcept {
    return *this == std::string_view{rhs};
  }
  template <std::size_t M>
  constexpr bool operator!=(const FortranString<M> &rhs) const noexcept {
    return !(*this == rhs);
  }
  constexpr bool operator!=(std::string_view rhs) const noexcept {
    return !(*this == rhs);
  }
  constexpr bool operator!=(const char *rhs) const noexcept {
    return !(*this == rhs);
  }

  // Three-way compare for ordering / sorting.  Padded with blanks.
  template <std::size_t M>
  constexpr auto operator<=>(const FortranString<M> &rhs) const noexcept {
    return detail::compare_padded(view(), rhs.view()) <=> 0;
  }
  constexpr auto operator<=>(std::string_view rhs) const noexcept {
    return detail::compare_padded(view(), rhs) <=> 0;
  }

  // ---- Concatenation (Fortran's "//" operator) -------------------------

  /// Result type of ``a // b`` is a FortranString of summed length —
  /// this is what Fortran's static-shape concatenation does.
  template <std::size_t M>
  constexpr FortranString<N + M>
  operator+(const FortranString<M> &rhs) const noexcept {
    FortranString<N + M> out;
    std::copy_n(data_.data(), N, out.data());
    std::copy_n(rhs.data(), M, out.data() + N);
    return out;
  }

private:
  template <std::size_t M> friend class FortranString;

  constexpr void assign_(std::string_view s) noexcept {
    const std::size_t take = std::min(s.size(), N);
    std::copy_n(s.data(), take, data_.data());
    for (std::size_t i = take; i < N; ++i) {
      data_[i] = ' ';
    }
  }

  std::array<char, N> data_{};
};

// ---- Free-function comparisons (let LHS be a literal) ---------------------

template <std::size_t N>
constexpr bool operator==(std::string_view a,
                          const FortranString<N> &b) noexcept {
  return b == a;
}
template <std::size_t N>
constexpr bool operator==(const char *a, const FortranString<N> &b) noexcept {
  return b == std::string_view{a};
}
template <std::size_t N>
constexpr bool operator!=(std::string_view a,
                          const FortranString<N> &b) noexcept {
  return !(b == a);
}

// ---- Stream insertion (mostly for tests / debugging) ----------------------

template <std::size_t N>
inline std::ostream &operator<<(std::ostream &os, const FortranString<N> &s) {
  return os.write(s.data(), static_cast<std::streamsize>(N));
}

/// List-directed ``read`` of a character variable: take the next
/// whitespace-delimited token (left-justified, blank-padded to width N).
template <std::size_t N>
inline std::istream &operator>>(std::istream &is, FortranString<N> &s) {
  std::string token;
  is >> token;
  s = std::string_view{token};
  return is;
}

// ---- Lexical string comparison intrinsics (LLT/LLE/LGT/LGE) ---------------
//
// Fortran's lexical-comparison intrinsics compare on the collating sequence
// with blank padding to the longer operand.  Any character actual converts
// to ``std::string_view``.
inline bool llt(std::string_view a, std::string_view b) noexcept {
  return detail::compare_padded(a, b) < 0;
}
inline bool lle(std::string_view a, std::string_view b) noexcept {
  return detail::compare_padded(a, b) <= 0;
}
inline bool lgt(std::string_view a, std::string_view b) noexcept {
  return detail::compare_padded(a, b) > 0;
}
inline bool lge(std::string_view a, std::string_view b) noexcept {
  return detail::compare_padded(a, b) >= 0;
}

// ---- Writable assumed-length character dummy ------------------------------

/// Non-owning, writable view of a character variable whose length the
/// *caller* fixes — the dummy form of an assumed-length ``CHARACTER*(*)``
/// the callee writes (the analog of ``ArrayRef`` for character data).
///
/// Reads convert to ``std::string_view``; assignment copies into the
/// caller's storage with Fortran blank-pad / truncate semantics.  A
/// read-only assumed-length dummy uses ``std::string_view`` directly; this
/// type is only for the writable (intent out / inout) case, so the body
/// can do ``out = ...`` and have the characters reach the caller.
class CharRef {
public:
  /// Null view — for a CHARACTER local that is only an alias for storage
  /// belonging to another ENTRY's dummy (never used on a live path).
  constexpr CharRef() noexcept : data_(nullptr), size_(0) {}
  constexpr CharRef(char *data, std::size_t size) noexcept
      : data_(data), size_(size) {}
  template <std::size_t N>
  constexpr CharRef(FortranString<N> &s) noexcept
      : data_(s.data()), size_(N) {}
  /// A read-only (intent(in)) FortranString actual.  All assumed-length
  /// dummies are CharRef, so a ``const`` character variable must bind too;
  /// the callee only reads it.
  template <std::size_t N>
  constexpr CharRef(const FortranString<N> &s) noexcept
      : data_(const_cast<char *>(s.data())), size_(N) {}
  /// View a read-only string (a literal, a concatenation temporary, or an
  /// intent(in) actual).  Valid for the duration of the call; the callee
  /// writes only when the actual is a genuine variable, so this covers the
  /// common Fortran pattern of passing any character expression.
  constexpr CharRef(std::string_view s) noexcept
      : data_(const_cast<char *>(s.data())), size_(s.size()) {}
  /// Direct ``std::string`` overload — a concatenation (``a // b``)
  /// produces a ``std::string`` rvalue, and C++ won't chain two
  /// user-defined conversions (string -> string_view -> CharRef), so this
  /// keeps such an actual viable for the call's duration.
  CharRef(const std::string &s) noexcept
      : data_(const_cast<char *>(s.data())), size_(s.size()) {}
  /// Bare string literal actual (``foo("ABC")``).
  CharRef(const char *s) noexcept
      : data_(const_cast<char *>(s)), size_(std::char_traits<char>::length(s)) {}

  // Assignment writes characters through to the viewed storage (pad /
  // truncate), so ``out = rhs`` behaves like Fortran character assignment
  // for every kind of right-hand side.  The copy-assignment likewise
  // copies characters (not the view).  A single constrained template
  // covers all string-viewable right-hand sides (FortranString, a
  // substring, std::string, a literal, string_view); taking the RHS by an
  // exact ``const S&`` makes it win over the copy-assignment for those
  // types, so ``out = fortranstring`` isn't ambiguous (it would be if both
  // an ``operator=(string_view)`` and ``operator=(const CharRef&)`` were
  // viable through a conversion).
  constexpr CharRef &operator=(const CharRef &o) noexcept {
    assign_(o.view());
    return *this;
  }
  template <typename S>
    requires(std::is_convertible_v<const S &, std::string_view> &&
             !std::is_same_v<std::remove_cvref_t<S>, CharRef>)
  constexpr CharRef &operator=(const S &s) noexcept {
    assign_(std::string_view(s));
    return *this;
  }

  constexpr operator std::string_view() const noexcept {
    return std::string_view{data_, size_};
  }
  constexpr std::string_view view() const noexcept {
    return std::string_view{data_, size_};
  }
  constexpr std::string_view trimmed() const noexcept {
    return detail::rstrip_blanks(view());
  }
  constexpr std::size_t size() const noexcept { return size_; }
  constexpr std::size_t len_trim() const noexcept { return trimmed().size(); }
  constexpr char *data() const noexcept { return data_; }

  /// 1-based character index.
  constexpr char &operator[](std::size_t one_based) const noexcept {
    return data_[one_based - 1];
  }
  /// 1-based inclusive substring ``s(lo:hi)`` — itself a writable view.
  constexpr CharRef operator()(std::size_t lo, std::size_t hi) const noexcept {
    return CharRef(data_ + (lo - 1), hi - lo + 1);
  }

  friend std::ostream &operator<<(std::ostream &os, const CharRef &s) {
    return os << s.view();
  }
  /// List-directed read into a CHARACTER variable: take the next token.
  friend std::istream &operator>>(std::istream &is, CharRef s) {
    std::string token;
    is >> token;
    s = std::string_view{token};
    return is;
  }
  /// Fortran character comparison (blank-padded).  ``==`` also yields
  /// ``!=`` in C++20.  A constrained template takes the other operand by
  /// an exact ``const S&`` (then views it), so it beats the std /
  /// FortranString string_view operators that would otherwise tie through
  /// CharRef's own string_view conversion; the (CharRef, CharRef) overload
  /// handles two views.
  friend bool operator==(const CharRef &a, const CharRef &b) noexcept {
    return detail::compare_padded(a.view(), b.view()) == 0;
  }
  template <typename S>
    requires(std::is_convertible_v<const S &, std::string_view> &&
             !std::is_same_v<std::remove_cvref_t<S>, CharRef>)
  friend bool operator==(const CharRef &a, const S &b) noexcept {
    return detail::compare_padded(a.view(), std::string_view(b)) == 0;
  }
  /// Fortran character ordering (blank-padded lexicographic), so a CHARACTER
  /// view participates in ``.LT.`` / ``.GT.`` comparisons.
  friend bool operator<(const CharRef &a, const CharRef &b) noexcept {
    return detail::compare_padded(a.view(), b.view()) < 0;
  }

private:
  constexpr void assign_(std::string_view s) noexcept {
    const std::size_t take = std::min(s.size(), size_);
    std::copy_n(s.data(), take, data_);
    for (std::size_t i = take; i < size_; ++i) {
      data_[i] = ' ';
    }
  }

  char *data_;
  std::size_t size_;
};

// Substring proxies -> CharRef (a substring used as an assumed-length
// CHARACTER actual).  Defined here, now that CharRef is complete.
template <std::size_t N>
inline FortranString<N>::Substring::operator CharRef() const noexcept {
  return CharRef(base_, size_);
}
template <std::size_t N>
inline FortranString<N>::ConstSubstring::operator CharRef() const noexcept {
  return CharRef(const_cast<char *>(base_), size_);
}


// ---- Character <-> integer intrinsics -------------------------------------

/// ACHAR(i) / CHAR(i): the length-1 character whose code is ``i``.
inline FortranString<1> achar(int i) noexcept {
  const char ch = static_cast<char>(i);
  return FortranString<1>(std::string_view{&ch, 1});
}

/// IACHAR(c) / ICHAR(c): the integer code of the first character of
/// ``c`` (0 for an empty string).
inline int ichar(std::string_view c) noexcept {
  return c.empty() ? 0 : static_cast<int>(static_cast<unsigned char>(c[0]));
}

/// REPEAT(string, ncopies): ``string`` concatenated ``ncopies`` times.
inline std::string repeat(std::string_view s, int ncopies) {
  std::string r;
  if (ncopies > 0) {
    r.reserve(s.size() * static_cast<std::size_t>(ncopies));
    for (int k = 0; k < ncopies; ++k) {
      r.append(s);
    }
  }
  return r;
}

/// SCAN(string, set[, back]): 1-based position of the first (or last, if
/// ``back``) character of ``string`` that appears in ``set``; 0 if none.
inline int scan(std::string_view s, std::string_view set,
                bool back = false) noexcept {
  if (back) {
    for (std::size_t i = s.size(); i-- > 0;) {
      if (set.find(s[i]) != std::string_view::npos) {
        return static_cast<int>(i + 1);
      }
    }
  } else {
    for (std::size_t i = 0; i < s.size(); ++i) {
      if (set.find(s[i]) != std::string_view::npos) {
        return static_cast<int>(i + 1);
      }
    }
  }
  return 0;
}

/// VERIFY(string, set[, back]): 1-based position of the first (or last,
/// if ``back``) character of ``string`` that is *not* in ``set``; 0 if
/// every character is in ``set``.
inline int verify(std::string_view s, std::string_view set,
                  bool back = false) noexcept {
  if (back) {
    for (std::size_t i = s.size(); i-- > 0;) {
      if (set.find(s[i]) == std::string_view::npos) {
        return static_cast<int>(i + 1);
      }
    }
  } else {
    for (std::size_t i = 0; i < s.size(); ++i) {
      if (set.find(s[i]) == std::string_view::npos) {
        return static_cast<int>(i + 1);
      }
    }
  }
  return 0;
}

} // namespace fortran

// std::format support: a FortranString formats like its (fixed-width)
// character view, honoring the usual string format spec.
template <std::size_t N>
struct std::formatter<fortran::FortranString<N>, char>
    : std::formatter<std::string_view, char> {
  template <typename FmtContext>
  auto format(const fortran::FortranString<N> &s, FmtContext &ctx) const {
    return std::formatter<std::string_view, char>::format(s.view(), ctx);
  }
};

#endif // FORTRAN_RT_STRING_HPP

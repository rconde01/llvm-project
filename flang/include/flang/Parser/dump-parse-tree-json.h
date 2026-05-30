//===-- include/flang/Parser/dump-parse-tree-json.h -------------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_PARSER_DUMP_PARSE_TREE_JSON_H_
#define FORTRAN_PARSER_DUMP_PARSE_TREE_JSON_H_

#include "char-block.h"
#include "dump-parse-tree.h"
#include "parse-tree-visitor.h"
#include "parse-tree.h"
#include "provenance.h"
#include "tools.h"
#include "unparse.h"
#include "flang/Common/indirection.h"
#include "flang/Semantics/attr.h"
#include "flang/Semantics/symbol.h"
#include "flang/Semantics/scope.h"
#include "flang/Semantics/type.h"
#include "flang/Evaluate/fold.h"
#include "flang/Evaluate/tools.h"
#include "llvm/Support/raw_ostream.h"
#include <cctype>
#include <string>
#include <type_traits>
#include <vector>

namespace Fortran::parser {

// JSON dumper for the parse tree.
//
// Each parse tree node is emitted as a JSON object of the form:
//   { "kind": "<name>",
//     "source": { "text": "...", "file": "...", "line": N, "col": N,
//                 "endLine": N, "endCol": N },
//     "fortran": "<analyzed Fortran text, when available>",
//     "children": [ ... ] }
//
// "source", "fortran", and "children" are emitted only when present.
//
// "Transparent" wrappers in the parse tree (CharBlock, Statement<T>,
// UnlabeledStatement<T>, common::Indirection<T>, std::tuple, std::variant)
// are not materialized as JSON nodes; their children appear directly as
// children of the enclosing node.
class ParseTreeJSONDumper {
public:
  explicit ParseTreeJSONDumper(llvm::raw_ostream &out,
      const AllCookedSources *allCooked = nullptr,
      const AnalyzedObjectsAsFortran *asFortran = nullptr)
      : out_(out), allCooked_{allCooked}, asFortran_{asFortran} {
    // Synthetic root level.  Pretending we are already inside a "children"
    // array means the first node we open emits no separator or array
    // opener, so the top-level JSON object stands alone.
    Level root;
    root.inChildrenArray = true;
    stack_.push_back(root);
  }

  // Generic visitor.  Every non-transparent parse tree node funnels through
  // these templates.  The node's name is obtained from ParseTreeDumper, which
  // already maintains a comprehensive NODE_NAME registry.
  template <typename T> bool Pre(const T &x) {
    OpenNode(ParseTreeDumper::GetNodeName(x));
    EmitOptionalSource(x);
    EmitOptionalFortran(x);
    EmitExprSemantics(x);
    return true;
  }

  template <typename T> void Post(const T &) { CloseNode(); }

  // Track the scope of the enclosing program unit so a Name can be told
  // apart as a local of this unit vs. host-associated state (e.g. a module
  // variable referenced in a contained procedure resolves directly to the
  // module symbol, with no association wrapper).
  bool Pre(const SubroutineStmt &x) {
    SetUnitScope(std::get<Name>(x.t));
    return Pre<SubroutineStmt>(x);
  }
  bool Pre(const FunctionStmt &x) {
    SetUnitScope(std::get<Name>(x.t));
    return Pre<FunctionStmt>(x);
  }
  // A main program / block data unit has no leading name-bearing stmt the
  // way a subprogram does (the PROGRAM statement is optional, and a block
  // data name is optional), so clear the scope on entry: otherwise the
  // unit inherits the previous subprogram's scope and every one of its
  // own variables is misreported as host-associated.  A named PROGRAM /
  // BLOCK DATA then sets the scope precisely.
  bool Pre(const MainProgram &x) {
    unitScope_ = nullptr;
    return Pre<MainProgram>(x);
  }
  bool Pre(const ProgramStmt &x) {
    SetUnitScope(x.v);
    return Pre<ProgramStmt>(x);
  }
  bool Pre(const BlockData &x) {
    unitScope_ = nullptr;
    return Pre<BlockData>(x);
  }
  bool Pre(const BlockDataStmt &x) {
    if (x.v) {
      SetUnitScope(*x.v);
    }
    return Pre<BlockDataStmt>(x);
  }

  // Transparent wrappers: do not produce a JSON node, just propagate.
  bool Pre(const CharBlock &) { return true; }
  void Post(const CharBlock &) {}

  // Statement wrappers carry useful source-range and label information that
  // would otherwise be discarded, so emit them as their own JSON nodes.
  template <typename T> bool Pre(const Statement<T> &x) {
    OpenNode("Statement");
    EmitSource(x.source);
    if (x.label) {
      out_ << ",\"label\":" << *x.label;
    }
    return true;
  }
  template <typename T> void Post(const Statement<T> &) { CloseNode(); }
  template <typename T> bool Pre(const UnlabeledStatement<T> &x) {
    OpenNode("UnlabeledStatement");
    EmitSource(x.source);
    return true;
  }
  template <typename T> void Post(const UnlabeledStatement<T> &) {
    CloseNode();
  }

  // A Name carries the resolved semantic Symbol after analysis.  Emit its
  // resolved type (e.g. "REAL(8)", "INTEGER(4)", "TYPE(point)") and rank so
  // the converter can read types directly — including those set by implicit
  // typing, custom IMPLICIT statements, KINDs, and host/use association —
  // rather than re-deriving them.
  bool Pre(const Name &x) {
    OpenNode("Name");
    EmitSource(x.source);
    out_ << ",\"fortran\":\"";
    EmitJSONString(x.source.ToString());
    out_ << "\"";
    if (x.symbol) {
      const semantics::Symbol &sym{x.symbol->GetUltimate()};
      if (const semantics::DeclTypeSpec * type{sym.GetType()}) {
        out_ << ",\"type\":\"";
        EmitJSONString(type->AsFortran());
        out_ << "\"";
      }
      out_ << ",\"rank\":" << sym.Rank();
      EmitShape(sym);
      // Classification facts so the converter need not guess whether a
      // ``name(...)`` is an array element or a function call, or whether a
      // referenced name is a local variable vs module/host state.
      if (sym.has<semantics::ObjectEntityDetails>()) {
        out_ << ",\"object\":true";
      }
      if (sym.IsSubprogram() || sym.has<semantics::ProcEntityDetails>() ||
          sym.attrs().test(semantics::Attr::EXTERNAL) ||
          sym.attrs().test(semantics::Attr::INTRINSIC)) {
        out_ << ",\"proc\":true";
      }
      // Resolved symbol attributes (INTENT, OPTIONAL, SAVE, POINTER,
      // ALLOCATABLE, TARGET, VALUE, EXTERNAL, INTRINSIC, PURE, ELEMENTAL,
      // ...).  These survive intermediate forms — a standalone
      // ``INTENT(IN) :: x`` decorates ``x`` just as ``REAL, INTENT(IN) :: x``
      // does — so consumers can read attributes off the resolved symbol
      // instead of walking parse-tree ``AttrSpec`` and missing the
      // standalone-statement forms.  Emitted as a JSON string array whose
      // entries are ``AttrToString`` lowercased — e.g.
      // ``"attrs":["intent(in)","optional"]``.  Only emitted when the
      // attribute set is non-empty.
      bool first{true};
      sym.attrs().IterateOverMembers([&](semantics::Attr a) {
        if (first) {
          out_ << ",\"attrs\":[";
          first = false;
        } else {
          out_ << ',';
        }
        std::string name{semantics::AttrToString(a)};
        for (char &c : name) {
          c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        }
        out_ << '"' << name << '"';
      });
      if (!first) {
        out_ << ']';
      }
      // Association: a name whose resolved symbol lives outside the
      // enclosing program unit's scope is module/host state, not a local.
      if (x.symbol->has<semantics::UseDetails>()) {
        out_ << ",\"assoc\":\"use\"";
      } else if (x.symbol->has<semantics::HostAssocDetails>()) {
        out_ << ",\"assoc\":\"host\"";
      } else if (unitScope_ && !OwnedBy(sym, *unitScope_)) {
        out_ << ",\"assoc\":\"host\"";
      }
    }
    return true;
  }

  template <typename T> bool Pre(const common::Indirection<T> &) {
    return true;
  }
  template <typename T> void Post(const common::Indirection<T> &) {}

  template <typename... A> bool Pre(const std::tuple<A...> &) { return true; }
  template <typename... A> void Post(const std::tuple<A...> &) {}

  template <typename... A> bool Pre(const std::variant<A...> &) { return true; }
  template <typename... A> void Post(const std::variant<A...> &) {}

  // Wrapper template nodes with no GetNodeName entry in ParseTreeDumper.
  template <typename A> bool Pre(const Scalar<A> &) {
    OpenNode("Scalar");
    return true;
  }
  template <typename A> void Post(const Scalar<A> &) { CloseNode(); }

  template <typename A> bool Pre(const Constant<A> &) {
    OpenNode("Constant");
    return true;
  }
  template <typename A> void Post(const Constant<A> &) { CloseNode(); }

  template <typename A> bool Pre(const Integer<A> &) {
    OpenNode("Integer");
    return true;
  }
  template <typename A> void Post(const Integer<A> &) { CloseNode(); }

  template <typename A> bool Pre(const Logical<A> &) {
    OpenNode("Logical");
    return true;
  }
  template <typename A> void Post(const Logical<A> &) { CloseNode(); }

  template <typename A> bool Pre(const DefaultChar<A> &) {
    OpenNode("DefaultChar");
    return true;
  }
  template <typename A> void Post(const DefaultChar<A> &) { CloseNode(); }

private:
  struct Level {
    // True once a child JSON node has been opened at this level.  Used to
    // know whether the next child needs a leading comma.
    bool hasChildren{false};
    // True once we have emitted the `, "children": [` opener at this level.
    bool inChildrenArray{false};
  };

  // Emit an explicit, constant-foldable array shape as
  // ``"shape":[[lo,hi],...]`` so the converter can size arrays from the
  // resolved symbol rather than re-reading DIMENSION/ArraySpec.  Folds
  // named-constant (PARAMETER) bounds; omitted when any bound is not a
  // compile-time constant.
  void EmitShape(const semantics::Symbol &sym) {
    const auto *obj{sym.detailsIf<semantics::ObjectEntityDetails>()};
    if (!obj) {
      return;
    }
    const semantics::ArraySpec &shape{obj->shape()};
    if (shape.empty() || !shape.IsExplicitShape()) {
      return;
    }
    std::string buf;
    llvm::raw_string_ostream ss{buf};
    ss << "[";
    bool first{true};
    for (const semantics::ShapeSpec &spec : shape) {
      auto lo{BoundValue(spec.lbound())};
      auto hi{BoundValue(spec.ubound())};
      if (!lo || !hi) {
        return; // not fully constant — omit the field
      }
      if (!first) {
        ss << ",";
      }
      first = false;
      ss << "[" << *lo << "," << *hi << "]";
    }
    ss << "]";
    out_ << ",\"shape\":" << buf;
  }

  static std::optional<std::int64_t> BoundValue(const semantics::Bound &b) {
    if (!b.isExplicit() || !b.GetExplicit()) {
      return std::nullopt;
    }
    return evaluate::ToInt64(*b.GetExplicit());
  }

  // Record the inner scope of the program unit named by ``x`` so later
  // Name visits can tell locals from host-associated state.
  void SetUnitScope(const Name &x) {
    if (x.symbol && x.symbol->scope()) {
      unitScope_ = x.symbol->scope();
    }
  }

  // True if ``sym`` is declared in ``scope`` or one of its ancestors up to
  // (but not crossing into) an enclosing module/program — i.e. it is a
  // genuine local/dummy of this unit rather than host/module state.
  static bool OwnedBy(
      const semantics::Symbol &sym, const semantics::Scope &unit) {
    return &sym.owner() == &unit;
  }

  void OpenNode(const std::string &name) { OpenNode(name.c_str()); }

  void OpenNode(const char *name) {
    Level &parent = stack_.back();
    if (!parent.inChildrenArray) {
      out_ << ",\"children\":[";
      parent.inChildrenArray = true;
    } else if (parent.hasChildren) {
      out_ << ",";
    }
    parent.hasChildren = true;
    out_ << "{\"kind\":\"";
    EmitJSONString(name);
    out_ << "\"";
    stack_.push_back({});
  }

  void CloseNode() {
    Level &top = stack_.back();
    if (top.inChildrenArray) {
      out_ << "]";
    }
    out_ << "}";
    stack_.pop_back();
  }

  // Emit a JSON-escaped string body (no surrounding quotes).
  void EmitJSONString(llvm::StringRef s) {
    for (char c : s) {
      switch (c) {
      case '"':
        out_ << "\\\"";
        break;
      case '\\':
        out_ << "\\\\";
        break;
      case '\b':
        out_ << "\\b";
        break;
      case '\f':
        out_ << "\\f";
        break;
      case '\n':
        out_ << "\\n";
        break;
      case '\r':
        out_ << "\\r";
        break;
      case '\t':
        out_ << "\\t";
        break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x",
              static_cast<unsigned>(static_cast<unsigned char>(c)));
          out_ << buf;
        } else {
          out_ << c;
        }
        break;
      }
    }
  }

  template <typename T> void EmitOptionalSource(const T &x) {
    if constexpr (HasSource<T>::value) {
      EmitSource(x.source);
    }
  }

  void EmitSource(const CharBlock &source) {
    if (source.empty()) {
      return;
    }
    out_ << ",\"source\":{\"text\":\"";
    EmitJSONString(source.ToString());
    out_ << "\"";
    if (allCooked_) {
      if (auto range{allCooked_->GetSourcePositionRange(source)}) {
        const SourcePosition &begin = range->first;
        const SourcePosition &end = range->second;
        out_ << ",\"file\":\"";
        EmitJSONString(begin.path.get());
        out_ << "\",\"line\":" << begin.line << ",\"col\":" << begin.column
             << ",\"endLine\":" << end.line << ",\"endCol\":" << end.column;
      }
    }
    out_ << "}";
  }

  // Mirrors ParseTreeDumper::AsFortran: emit a "fortran" string when the node
  // carries semantic information that can be rendered as Fortran source.
  template <typename T> void EmitOptionalFortran(const T &x) {
    std::string buf;
    llvm::raw_string_ostream ss{buf};
    if constexpr (HasTypedExpr<T>::value) {
      if (asFortran_ && x.typedExpr) {
        asFortran_->expr(ss, *x.typedExpr);
      }
    } else if constexpr (std::is_same_v<T, AssignmentStmt> ||
        std::is_same_v<T, PointerAssignmentStmt>) {
      if (asFortran_ && x.typedAssignment) {
        asFortran_->assignment(ss, *x.typedAssignment);
      }
    } else if constexpr (std::is_same_v<T, CallStmt>) {
      if (asFortran_ && x.typedCall) {
        asFortran_->call(ss, *x.typedCall);
      }
    } else if constexpr (std::is_same_v<T, IntLiteralConstant> ||
        std::is_same_v<T, SignedIntLiteralConstant> ||
        std::is_same_v<T, UnsignedLiteralConstant>) {
      ss << std::get<CharBlock>(x.t);
    } else if constexpr (std::is_same_v<T, RealLiteralConstant::Real>) {
      ss << x.source;
    } else if constexpr (std::is_same_v<T, std::string> ||
        std::is_same_v<T, std::int64_t> || std::is_same_v<T, std::uint64_t>) {
      ss << x;
    } else if constexpr (std::is_same_v<T, Name>) {
      ss << x.source.ToString();
    } else if constexpr (std::is_same_v<T, const char *>) {
      // A source Location (e.g. an IMPLICIT LetterSpec range bound) — emit
      // the single character it points at.
      if (x) {
        ss << *x;
      }
    } else if constexpr (std::is_same_v<T, int>) {
      ss << x;
    } else if constexpr (std::is_same_v<T, bool>) {
      ss << (x ? "true" : "false");
    }
    if (ss.tell() == 0) {
      return;
    }
    out_ << ",\"fortran\":\"";
    EmitJSONString(buf);
    out_ << "\"";
  }

  // Emit the resolved semantic facts (type, rank, expression category,
  // folded scalar integer value) for any parse-tree node that carries an
  // analyzed expression — ``Expr``, ``Variable``, ``DataStmtConstant``,
  // ``AllocateObject``, ``PointerObject``.  These let consumers read
  // directly off the parse tree what semantics already computed instead
  // of re-deriving it from node shape.
  //
  // ``category`` distinguishes assignable designators (``variable``) from
  // computed-value expressions, with ``constant`` reserved for ones that
  // evaluate folded to a known compile-time value.  ``value`` carries the
  // folded integer when the expression is a scalar integer constant — the
  // common case for array bounds, kind expressions and PARAMETER constants.
  template <typename T> void EmitExprSemantics(const T &x) {
    if constexpr (HasTypedExpr<T>::value) {
      if (!x.typedExpr || !x.typedExpr->v) {
        return;
      }
      const auto &e{*x.typedExpr->v};
      if (auto dynType{e.GetType()}) {
        out_ << ",\"type\":\"";
        EmitJSONString(dynType->AsFortran());
        out_ << "\"";
      }
      out_ << ",\"rank\":" << e.Rank();
      const char *cat;
      if (evaluate::IsVariable(e)) {
        cat = "variable";
      } else if (evaluate::IsConstantExpr(e)) {
        cat = "constant";
      } else {
        cat = "expression";
      }
      out_ << ",\"category\":\"" << cat << "\"";
      if (auto v{evaluate::ToInt64(e)}) {
        out_ << ",\"value\":\"" << *v << "\"";
      }
    }
  }

  llvm::raw_ostream &out_;
  const AllCookedSources *const allCooked_;
  const AnalyzedObjectsAsFortran *const asFortran_;
  std::vector<Level> stack_;
  const semantics::Scope *unitScope_{nullptr};
};

template <typename T>
llvm::raw_ostream &DumpTreeJSON(llvm::raw_ostream &out, const T &x,
    const AllCookedSources *allCooked = nullptr,
    const AnalyzedObjectsAsFortran *asFortran = nullptr) {
  ParseTreeJSONDumper dumper{out, allCooked, asFortran};
  Walk(x, dumper);
  out << "\n";
  return out;
}

} // namespace Fortran::parser

#endif // FORTRAN_PARSER_DUMP_PARSE_TREE_JSON_H_

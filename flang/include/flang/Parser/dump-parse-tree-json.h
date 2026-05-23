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
#include "llvm/Support/raw_ostream.h"
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
    return true;
  }

  template <typename T> void Post(const T &) { CloseNode(); }

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

  llvm::raw_ostream &out_;
  const AllCookedSources *const allCooked_;
  const AnalyzedObjectsAsFortran *const asFortran_;
  std::vector<Level> stack_;
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

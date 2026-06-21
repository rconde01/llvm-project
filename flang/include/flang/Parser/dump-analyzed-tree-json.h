//===-- include/flang/Parser/dump-analyzed-tree-json.h -------------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#ifndef FORTRAN_PARSER_DUMP_ANALYZED_TREE_JSON_H_
#define FORTRAN_PARSER_DUMP_ANALYZED_TREE_JSON_H_

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
#include "llvm/Frontend/OpenMP/OMP.h"
#include "llvm/Support/raw_ostream.h"
#include <cctype>
#include <string>
#include <type_traits>
#include <vector>

namespace Fortran::parser {

// JSON dumper for the *analyzed* parse tree.
//
// Unlike the text dumper (``-fdebug-dump-parse-tree``), which emits parse
// tree structure only, this dumper bundles the parse tree with the
// resolved-symbol facts and analyzed-expression facts that semantics
// computed — so a downstream tool reads structure and semantics in one
// pass.  Each Name carries its resolved type/rank/shape, classification
// (object/proc/assoc), full attribute set, implicit-typing flag, and —
// when applicable — its declaring source location, owning derived type,
// COMMON-block name, EQUIVALENCE-class index, and resolved procedure-
// interface name.  Each node with an analyzed ``typedExpr`` (Expr /
// Variable / DataStmtConstant / AllocateObject / PointerObject) carries
// the expression's type, rank, category (variable / constant /
// expression), and folded scalar value (decimal-string for integers,
// Fortran rendering for other scalar constants).
//
// See ``flang/docs/AnalyzedTreeJSONDumper.md`` for the full schema.
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
class AnalyzedTreeJSONDumper {
public:
  explicit AnalyzedTreeJSONDumper(llvm::raw_ostream &out,
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
  // consumers can read types directly — including those set by implicit
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
        // Polymorphism marker: ``CLASS(t)`` / ``CLASS(*)`` / ``TYPE(*)``
        // -- the ``type`` string already encodes the spelling, but
        // ``polymorphic`` and ``unlimited_polymorphic`` let a consumer
        // branch on the property directly without parsing the spelling.
        if (type->IsPolymorphic()) {
          out_ << ",\"polymorphic\":true";
        }
        if (type->IsUnlimitedPolymorphic()) {
          out_ << ",\"unlimited_polymorphic\":true";
        }
      }
      out_ << ",\"rank\":" << sym.Rank();
      EmitShape(sym);
      // Classification facts so consumers need not guess whether a
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
      // Implicit typing: a symbol whose type came from IMPLICIT rules
      // (rather than an explicit declaration) carries Flag::Implicit.
      // Reformatting tools want this to tell apart names that need a
      // generated explicit declaration when adding IMPLICIT NONE.
      if (sym.test(semantics::Symbol::Flag::Implicit)) {
        out_ << ",\"implicit\":true";
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
      // Defining source location.  ``sym.name()`` is the SourceName
      // (CharBlock) of the symbol's declaring occurrence.  Emit it when
      // this Name is a *use* (i.e. its own source range differs from the
      // declaration's), so jump-to-definition tools can resolve names
      // without rebuilding a symbol table.
      const parser::CharBlock &defined{sym.name()};
      if (!defined.empty() && defined.begin() != x.source.begin()) {
        EmitDefinedAt(defined);
      }
      // Owner of a component: when the symbol's scope is a derived type,
      // emit the type's name so a tool walking ``rec%field`` can ask "what
      // type owns ``field``?" without traversing the parent ``DataRef``.
      if (sym.owner().IsDerivedType()) {
        if (const semantics::Symbol *owner{sym.owner().symbol()}) {
          out_ << ",\"defined_in\":\"";
          EmitJSONString(owner->name().ToString());
          out_ << "\"";
        }
      }
      // COMMON-block membership for an Object: the consumer doesn't have
      // to walk the parse-tree COMMON statements to know which block a
      // variable lives in.
      if (const auto *obj{sym.detailsIf<semantics::ObjectEntityDetails>()}) {
        if (const semantics::Symbol * cb{obj->commonBlock()}) {
          out_ << ",\"common_block\":\"";
          EmitJSONString(cb->name().ToString());
          out_ << "\"";
        }
      }
      // EQUIVALENCE-class index: 0-based position within the owning
      // scope's equivalenceSets() list.  ``equivalence_class`` lets a
      // storage-analysis tool group co-aliased variables in one pass.
      if (sym.has<semantics::ObjectEntityDetails>()) {
        std::size_t setIndex{0};
        bool found{false};
        for (const semantics::EquivalenceSet &set : sym.owner().equivalenceSets()) {
          for (const semantics::EquivalenceObject &eo : set) {
            if (&eo.symbol == &sym) {
              found = true;
              break;
            }
          }
          if (found) {
            break;
          }
          ++setIndex;
        }
        if (found) {
          out_ << ",\"equivalence_class\":" << setIndex;
        }
      }
      // Procedure-interface link: a ``procedure(iface), pointer :: p``
      // declares ``p`` whose ProcEntityDetails carries the resolved
      // interface symbol.  Emit the interface's name so cross-reference
      // tools can resolve the indirect call without searching for an
      // interface block by hand.
      if (const auto *pe{sym.detailsIf<semantics::ProcEntityDetails>()}) {
        if (const semantics::Symbol * iface{pe->procInterface()}) {
          out_ << ",\"proc_interface\":\"";
          EmitJSONString(iface->name().ToString());
          out_ << "\"";
        }
      }
      // Storage size and offset.  Set by semantics for objects whose
      // size is known at compile time -- emit non-zero values only so
      // the JSON stays compact for symbols that aren't laid out (e.g.
      // assumed-shape dummies, deferred-length CHARACTER).  Useful for
      // ABI/debug-info tools and for converters that need byte-level
      // layout without re-computing it.
      if (sym.size() > 0) {
        out_ << ",\"size\":" << sym.size();
      }
      if (sym.offset() > 0) {
        out_ << ",\"offset\":" << sym.offset();
      }
      // Source module for use-associated names.  Resolved via
      // ``UseDetails::symbol().owner()`` -> the owning scope is the
      // module's own scope, whose ``symbol()`` is the module symbol.
      // Lets a downstream tool answer "where does this name come from?"
      // without walking USE statements.
      if (const auto *use{x.symbol->detailsIf<semantics::UseDetails>()}) {
        const semantics::Scope &owner{use->symbol().owner()};
        if (const semantics::Symbol * mod{owner.symbol()}) {
          out_ << ",\"from_module\":\"";
          EmitJSONString(mod->name().ToString());
          out_ << "\"";
        }
      }
      // BIND(C, NAME="cname") symbols carry a C name distinct from the
      // Fortran name.  Both ObjectEntity and Subprogram details classes
      // can hold a bindName; expose whichever is set.
      if (const std::string * bn{GetBindName(sym)}) {
        if (!bn->empty()) {
          out_ << ",\"bind_name\":\"";
          EmitJSONString(*bn);
          out_ << "\"";
        }
      }
      // For a COMMON-block-name symbol (the ``/blk/`` in ``COMMON /blk/
      // x, y``), emit the ordered member list, byte sizes, and any
      // declared alignment.  Lets a binary-tooling consumer reconstruct
      // the block layout without walking CommonStmt nodes per routine.
      if (const auto *cb{sym.detailsIf<semantics::CommonBlockDetails>()}) {
        EmitCommonBlockLayout(*cb);
      }
      // Procedure signature: for a SUBROUTINE / FUNCTION symbol, emit the
      // ordered dummy-argument list (with each dummy's intent, type, rank,
      // optionality) plus the function result.  Lets a call-site analyzer
      // resolve overload / actual-to-dummy matching without walking the
      // routine's specification part.
      if (const auto *sp{sym.detailsIf<semantics::SubprogramDetails>()}) {
        EmitProcedureSignature(*sp);
      }
      // Derived-type component summary: ordered component list with
      // per-component type / rank / size / offset.  Mirrors the
      // ``common_block_layout`` field for derived types.
      if (const auto *dt{sym.detailsIf<semantics::DerivedTypeDetails>()}) {
        EmitDerivedTypeLayout(sym, *dt);
        // FINAL subroutines bound to the type, in the order semantics
        // recorded them.  Each name is the bound subprogram's symbol
        // name.  Emitting this here mirrors ``binds_to`` for ordinary
        // type-bound procedures.
        if (!dt->finals().empty()) {
          out_ << ",\"finals\":[";
          bool first{true};
          for (const auto &kv : dt->finals()) {
            if (!first) {
              out_ << ',';
            }
            first = false;
            out_ << "\"";
            EmitJSONString(kv.second->name().ToString());
            out_ << "\"";
          }
          out_ << "]";
        }
        if (dt->sequence()) {
          out_ << ",\"sequence_type\":true";
        }
      }
      // Generic-interface resolution: a generic name (an INTERFACE block,
      // a defined-operator generic, or a type-bound generic) carries the
      // list of specific procedures plus the optional same-named specific
      // / derivedType companions.  Emitting these lets a tool resolve
      // generic invocations without searching for the interface block.
      if (const auto *gn{sym.detailsIf<semantics::GenericDetails>()}) {
        EmitGeneric(*gn);
      }
      // USE-association rename: a ``use mm, foo => bar`` brings ``bar``
      // in as ``foo`` -- the local Symbol is named ``foo`` but its
      // UseDetails points at ``bar``.  Surface the source name when it
      // differs so a tool can map back to the originating declaration.
      if (const auto *use{x.symbol->detailsIf<semantics::UseDetails>()}) {
        const std::string from{use->symbol().name().ToString()};
        if (from != x.source.ToString()) {
          out_ << ",\"from_name\":\"";
          EmitJSONString(from);
          out_ << "\"";
        }
      }
      // Type-bound procedure binding: ``procedure(iface), pass :: meth =>
      // implementation`` -- the binding's symbol resolves to the
      // implementation procedure.  ``binds_to`` lets a tool resolve
      // ``obj%meth`` calls without rewalking the type-bound procedure
      // statements.
      if (const auto *pb{sym.detailsIf<semantics::ProcBindingDetails>()}) {
        out_ << ",\"binds_to\":\"";
        EmitJSONString(pb->symbol().name().ToString());
        out_ << "\"";
        // ``PASS(arg)`` selects which dummy receives the passed-object;
        // omit on ``NOPASS`` and on default PASS (the binding still
        // takes the first dummy).
        if (auto pn{pb->passName()}) {
          out_ << ",\"pass_name\":\"";
          EmitJSONString(pn->ToString());
          out_ << "\"";
        }
      }
      // NAMELIST membership: a NAMELIST-group symbol's NamelistDetails
      // carries its object list in declared order.  Emit as a name array
      // so a formatted I/O tool can enumerate group members directly.
      if (const auto *nl{sym.detailsIf<semantics::NamelistDetails>()}) {
        if (!nl->objects().empty()) {
          out_ << ",\"namelist_objects\":[";
          bool first{true};
          for (const semantics::Symbol &obj : nl->objects()) {
            if (!first) {
              out_ << ',';
            }
            first = false;
            out_ << "\"";
            EmitJSONString(obj.name().ToString());
            out_ << "\"";
          }
          out_ << "]";
        }
      }
      // ASSOCIATE / SELECT TYPE / SELECT RANK construct entities: the
      // associated expression and (for SELECT RANK) the case's rank.
      // Lets a refactoring tool resolve construct names without walking
      // the AssociateStmt / TypeGuardStmt nodes.
      if (const auto *ae{sym.detailsIf<semantics::AssocEntityDetails>()}) {
        if (ae->expr()) {
          std::string buf;
          llvm::raw_string_ostream ss{buf};
          ae->expr()->AsFortran(ss);
          ss.flush();
          if (!buf.empty()) {
            out_ << ",\"assoc_expr\":\"";
            EmitJSONString(buf);
            out_ << "\"";
          }
        }
        if (auto r{ae->rank()}) {
          out_ << ",\"assoc_rank\":" << *r;
        }
        if (ae->IsAssumedRank()) {
          out_ << ",\"assoc_rank\":\"*\"";
        }
        if (ae->isTypeGuard()) {
          out_ << ",\"type_guard\":true";
        }
      }
      // Initial value: an Object's ``init()`` (the analyzed
      // initialization expression) carries the resolved RHS of a
      // ``REAL :: a = 3.14`` or ``PARAMETER :: pi = 3.14`` form.  Emit
      // its Fortran rendering so a tool gets the constant directly,
      // without re-running expression analysis on the declaration's
      // Initialization node.
      // Module / submodule classification.  ``module:true`` on every
      // module-scope symbol so a tool can quickly filter modules from
      // ordinary subprograms; ``submodule:true`` on submodules with the
      // parent module name available via ``parent_module``.
      if (const auto *md{sym.detailsIf<semantics::ModuleDetails>()}) {
        out_ << ",\"module\":true";
        if (md->isSubmodule()) {
          out_ << ",\"submodule\":true";
          if (const semantics::Scope * parent{md->parent()}) {
            if (const semantics::Symbol * ps{parent->symbol()}) {
              out_ << ",\"parent_module\":\"";
              EmitJSONString(ps->name().ToString());
              out_ << "\"";
            }
          }
        }
      }
      if (const auto *obj{sym.detailsIf<semantics::ObjectEntityDetails>()}) {
        if (obj->init()) {
          std::string buf;
          llvm::raw_string_ostream ss{buf};
          obj->init()->AsFortran(ss);
          ss.flush();
          if (!buf.empty()) {
            out_ << ",\"init\":\"";
            EmitJSONString(buf);
            out_ << "\"";
          }
        }
      }
    }
    return true;
  }

  // OpenMP directive: surface the directive-name string (``"parallel
  // do"`` / ``"target teams"`` / ...) so a tool reading the JSON can
  // tell what construct is in front of it without consulting an
  // OpenMP-version table.
  bool Pre(const OmpDirectiveName &x) {
    OpenNode("OmpDirectiveName");
    EmitSource(x.source);
    llvm::StringRef name{llvm::omp::getOpenMPDirectiveName(x.v,
        llvm::omp::FallbackVersion)};
    if (!name.empty()) {
      out_ << ",\"directive\":\"";
      EmitJSONString(name);
      out_ << "\"";
    }
    return true;
  }
  void Post(const OmpDirectiveName &) { CloseNode(); }

  // OpenMP clause: surface the clause-name discriminant on the
  // OmpClause node so consumers can tell ``reduction`` from
  // ``schedule`` without inspecting the child's parse-tree class.
  bool Pre(const OmpClause &x) {
    OpenNode("OmpClause");
    EmitSource(x.source);
    llvm::StringRef name{llvm::omp::getOpenMPClauseName(x.Id(),
        llvm::omp::FallbackVersion)};
    if (!name.empty()) {
      out_ << ",\"clause\":\"";
      EmitJSONString(name);
      out_ << "\"";
    }
    return true;
  }
  void Post(const OmpClause &) { CloseNode(); }

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
  // ``"shape":[[lo,hi],...]`` so consumers can size arrays from the
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

  // BIND(C, NAME="...") on an object, subprogram, or common block carries
  // a C name distinct from the Fortran name.  All three details classes
  // expose ``bindName()`` via ``WithBindName``, returning a
  // ``const std::string*`` (nullptr if no BIND name was set).
  static const std::string *GetBindName(const semantics::Symbol &sym) {
    if (const auto *obj{sym.detailsIf<semantics::ObjectEntityDetails>()}) {
      return obj->bindName();
    }
    if (const auto *sp{sym.detailsIf<semantics::SubprogramDetails>()}) {
      return sp->bindName();
    }
    if (const auto *cb{sym.detailsIf<semantics::CommonBlockDetails>()}) {
      return cb->bindName();
    }
    return nullptr;
  }

  // Emit a COMMON-block-name symbol's layout as a JSON sub-object.
  // ``common_block_layout`` carries ``alignment`` (when set) and an
  // ``objects`` array of ``{name, size, offset}`` triples in declared
  // order.  Lets a binary tool reconstruct the block without walking
  // the parse-tree ``CommonStmt`` per routine.
  void EmitCommonBlockLayout(const semantics::CommonBlockDetails &cb) {
    out_ << ",\"common_block_layout\":{";
    bool sep{false};
    if (cb.alignment() > 0) {
      out_ << "\"alignment\":" << cb.alignment();
      sep = true;
    }
    if (!cb.objects().empty()) {
      if (sep) {
        out_ << ',';
      }
      out_ << "\"objects\":[";
      bool first{true};
      for (const semantics::MutableSymbolRef &ref : cb.objects()) {
        const semantics::Symbol &m{*ref};
        if (!first) {
          out_ << ',';
        }
        first = false;
        out_ << "{\"name\":\"";
        EmitJSONString(m.name().ToString());
        out_ << "\"";
        if (m.size() > 0) {
          out_ << ",\"size\":" << m.size();
        }
        if (m.offset() > 0) {
          out_ << ",\"offset\":" << m.offset();
        }
        out_ << "}";
      }
      out_ << "]";
    }
    out_ << "}";
  }

  // Emit a per-symbol fact block for one dummy or result: name, intent
  // (when the symbol carries an INTENT attribute), type, rank, and the
  // OPTIONAL / VALUE / POINTER / ALLOCATABLE / TARGET flags relevant to
  // call-site reasoning.  Used by both ``dummy_args`` and ``result``.
  void EmitArgumentObject(const semantics::Symbol &arg) {
    out_ << "{\"name\":\"";
    EmitJSONString(arg.name().ToString());
    out_ << "\"";
    if (const semantics::DeclTypeSpec * type{arg.GetType()}) {
      out_ << ",\"type\":\"";
      EmitJSONString(type->AsFortran());
      out_ << "\"";
    }
    out_ << ",\"rank\":" << arg.Rank();
    const auto &attrs{arg.attrs()};
    static constexpr semantics::Attr kReportedAttrs[] = {
        semantics::Attr::INTENT_IN,
        semantics::Attr::INTENT_OUT,
        semantics::Attr::INTENT_INOUT,
        semantics::Attr::OPTIONAL,
        semantics::Attr::VALUE,
        semantics::Attr::POINTER,
        semantics::Attr::ALLOCATABLE,
        semantics::Attr::TARGET,
    };
    bool first{true};
    for (semantics::Attr a : kReportedAttrs) {
      if (!attrs.test(a)) {
        continue;
      }
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
    }
    if (!first) {
      out_ << ']';
    }
    out_ << "}";
  }

  // Emit ``procedure:{...}`` summarizing a subprogram symbol's signature.
  // Always carries ``is_function`` so a tool can disambiguate the two
  // major shapes; ``dummy_args`` and ``result`` are emitted only when
  // present (``result`` only on functions; alternate-return dummies
  // appear as ``{"alternate_return":true}`` placeholders).
  void EmitProcedureSignature(const semantics::SubprogramDetails &sp) {
    out_ << ",\"procedure\":{";
    out_ << "\"is_function\":" << (sp.isFunction() ? "true" : "false");
    if (!sp.dummyArgs().empty()) {
      out_ << ",\"dummy_args\":[";
      bool first{true};
      for (const semantics::Symbol *arg : sp.dummyArgs()) {
        if (!first) {
          out_ << ',';
        }
        first = false;
        if (arg) {
          EmitArgumentObject(*arg);
        } else {
          out_ << "{\"alternate_return\":true}";
        }
      }
      out_ << "]";
    }
    if (sp.isFunction()) {
      out_ << ",\"result\":";
      EmitArgumentObject(sp.result());
    }
    out_ << "}";
  }

  // Emit ``components:[{name,type,rank,offset?,size?}, ...]`` for a
  // derived-type symbol, walking the type's scope in declared component
  // order (``DerivedTypeDetails::componentNames()`` preserves the
  // declared order, including a parent component first if present).
  void EmitDerivedTypeLayout(const semantics::Symbol &typeSym,
      const semantics::DerivedTypeDetails &dt) {
    if (dt.componentNames().empty() || !typeSym.scope()) {
      return;
    }
    out_ << ",\"components\":[";
    bool first{true};
    for (const parser::CharBlock &name : dt.componentNames()) {
      auto it{typeSym.scope()->find(name)};
      if (it == typeSym.scope()->end()) {
        continue;
      }
      const semantics::Symbol &c{*it->second};
      if (!first) {
        out_ << ',';
      }
      first = false;
      out_ << "{\"name\":\"";
      EmitJSONString(c.name().ToString());
      out_ << "\"";
      if (const semantics::DeclTypeSpec * type{c.GetType()}) {
        out_ << ",\"type\":\"";
        EmitJSONString(type->AsFortran());
        out_ << "\"";
      }
      out_ << ",\"rank\":" << c.Rank();
      if (c.size() > 0) {
        out_ << ",\"size\":" << c.size();
      }
      if (c.offset() > 0) {
        out_ << ",\"offset\":" << c.offset();
      }
      out_ << "}";
    }
    out_ << "]";
  }

  // Emit ``generic:{kind, specifics:[name,...], specific?, derived_type?}``
  // for a generic-interface symbol.  ``kind`` is the GenericKind string
  // (``"Name"``, ``"DefinedOp"``, an operator spelling, etc.) so a
  // consumer can tell a regular generic apart from a defined-operator
  // generic or an I/O generic.
  void EmitGeneric(const semantics::GenericDetails &gn) {
    out_ << ",\"generic\":{";
    out_ << "\"kind\":\"";
    EmitJSONString(gn.kind().ToString());
    out_ << "\"";
    if (!gn.specificProcs().empty()) {
      out_ << ",\"specifics\":[";
      bool first{true};
      for (const semantics::Symbol &sp : gn.specificProcs()) {
        if (!first) {
          out_ << ',';
        }
        first = false;
        out_ << "\"";
        EmitJSONString(sp.name().ToString());
        out_ << "\"";
      }
      out_ << "]";
    }
    if (const semantics::Symbol * s{gn.specific()}) {
      out_ << ",\"specific\":\"";
      EmitJSONString(s->name().ToString());
      out_ << "\"";
    }
    if (const semantics::Symbol * d{gn.derivedType()}) {
      out_ << ",\"derived_type\":\"";
      EmitJSONString(d->name().ToString());
      out_ << "\"";
    }
    out_ << "}";
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

  // Same shape as ``source`` but emitted as ``defined_at`` for a Name's
  // declaration site (pointing back at the symbol from a use site).
  void EmitDefinedAt(const CharBlock &source) {
    if (source.empty()) {
      return;
    }
    out_ << ",\"defined_at\":{\"text\":\"";
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
      bool isConstant{false};
      if (evaluate::IsVariable(e)) {
        cat = "variable";
      } else if (evaluate::IsConstantExpr(e)) {
        cat = "constant";
        isConstant = true;
      } else {
        cat = "expression";
      }
      out_ << ",\"category\":\"" << cat << "\"";
      // ``value`` for a scalar integer constant -- the common case for
      // array bounds, kinds, and PARAMETER values.
      if (auto v{evaluate::ToInt64(e)}) {
        out_ << ",\"value\":\"" << *v << "\"";
      } else if (isConstant && e.Rank() == 0) {
        // Render any other scalar constant (REAL, LOGICAL, CHARACTER,
        // COMPLEX) via the expression's own Fortran rendering.  The
        // separator-less form is what a tool wants when extracting
        // PARAMETER tables; the ``fortran`` field nearby duplicates the
        // analyzed-source spelling for round-trip readability.
        std::string buf;
        llvm::raw_string_ostream ss{buf};
        e.AsFortran(ss);
        ss.flush();
        if (!buf.empty()) {
          out_ << ",\"value\":\"";
          EmitJSONString(buf);
          out_ << "\"";
        }
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
  AnalyzedTreeJSONDumper dumper{out, allCooked, asFortran};
  Walk(x, dumper);
  out << "\n";
  return out;
}

} // namespace Fortran::parser

#endif // FORTRAN_PARSER_DUMP_ANALYZED_TREE_JSON_H_

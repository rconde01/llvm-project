#ifndef FORTRAN_PARSER_ROB_DUMP_H_
#define FORTRAN_PARSER_ROB_DUMP_H_

#include "flang/Common/idioms.h"
#include "flang/Common/indirection.h"
#include "flang/Frontend/CompilerInstance.h"
#include "flang/Parser/format-specification.h"
#include "flang/Parser/parse-tree-visitor.h"
#include "flang/Parser/parse-tree.h"
#include "flang/Parser/tools.h"
#include "flang/Parser/unparse.h"
#include "flang/Support/Fortran.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/Frontend/OpenMP/OMP.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/raw_ostream.h"
#include <string>
#include <type_traits>

namespace Fortran::parser {

struct Context {
  std::unordered_map<std::string, int> sources;
};

llvm::json::Value toJSON(Fortran::frontend::CompilerInstance &ci,
    Context &context, std::string const &label,
    Fortran::parser::Name const &name) {

  llvm::json::Object Result{{"type", "Name"}, {"name", name.ToString()}};

  auto positionRange =
      ci.getAllCookedSources().GetSourcePositionRange(name.source);

  if (positionRange) {
    auto &start = positionRange->first;
    auto &end = positionRange->second;

    assert(start.sourceFile->path() == end.sourceFile->path() &&
        "SourcePosition range spans multiple source files");

    auto itr = context.sources.find(start.sourceFile->path());

    if (itr == context.sources.end())
      itr = context.sources
                .emplace(start.sourceFile->path(), context.sources.size() + 1)
                .first;

    llvm::json::Object sourceRange =
        llvm::json::Object{{"sourceFile", itr->second},
            {"startLine", start.line}, {"startColumn", start.column},
            {"endLine", end.line}, {"endColumn", end.column}};

    if (start.path != start.sourceFile->path()) {
      itr = context.sources.find(start.path);

      if (itr == context.sources.end())
        itr = context.sources.emplace(start.path, context.sources.size() + 1)
                  .first;

      sourceRange["sourcePath"] = itr->second;
    }

    if (start.line != start.trueLineNumber)
      sourceRange["trueStartLine"] = start.trueLineNumber;

    if (end.line != end.trueLineNumber)
      sourceRange["trueEndLine"] = end.trueLineNumber;

    Result["sourceRange"] = std::move(sourceRange);
  }

  return Result;
}


llvm::json::Value toJSON(Fortran::frontend::CompilerInstance &ci,
    Context &context, Fortran::parser::Program const &program) {
  llvm::json::Object Result{{"type", "Program"}};

  llvm::json::Array units;

  for (const auto &unit : program.getUnits()) {
    std::visit(
        [&](auto const &x) { units.push_back(toJSON(ci, context, "unit", x)); },
        unit);

    units.push_back(toJSON(ci, context, "unit", *unit));
  }

  Result["units"] = std::move(units);

  llvm::json::Array commonBlocks;

  for (const auto &commonBlock : program.getCommonBlocks())
    commonBlocks.push_back(toJSON(ci, context, "commonBlock", commonBlock));

  Result["commonBlocks"] = std::move(commonBlocks);

  // TODO: scope variable list?
  // TODO: Source map

  return Result;
}

class RobDump {
public:
  explicit RobDump(
      llvm::json::Object &object, Fortran::frontend::CompilerInstance &ci)
      : obj_(object), ci_{ci} {}

  template <typename T> std::string AsFortran(const T &x) {
    return "";
    // std::string{typeid(T).name()} + "\n";
  }

  std::string to_string(SourcePosition pos) const {
    std::string buf;
    llvm::raw_string_ostream ss{buf};
    ss << "line " << pos.line << ", column " << pos.column
       << pos.sourceFile->path();
    return buf;
  }

  std::string to_string(const Name &name) const {
    std::string buf;
    llvm::raw_string_ostream ss{buf};

    ss << name.ToString() << "\n";

    auto positionRange =
        ci_.getAllCookedSources().GetSourcePositionRange(name.source);

    ss << "    " << to_string(positionRange->first) << "\n";
    ss << "    " << to_string(positionRange->second) << "\n";

    return buf;
  }

  template <> std::string AsFortran(const SubroutineStmt &x) {
    std::string buf;
    llvm::raw_string_ostream ss{buf};

    auto &name = std::get<Name>(x.t);
    auto &args = std::get<std::list<DummyArg>>(x.t);

    ss << "SUBROUTINE " << to_string(name) << "\n";

    for (auto &arg : args) {
      std::visit(llvm::makeVisitor(
                     [&](Name const &name) { ss << "  " << to_string(name); },
                     [&](Star const &) { ss << "  *"; }),
          arg.u);
      ss << "\n";
    }

    return ss.str();
  }

  template <typename T> bool Pre(const T &x) {
    std::string fortran{AsFortran(x)};

    // out_ << fortran;

    return true;
  }

  template <typename T> void Post(const T &x) {}

private:
  llvm::json::Object &obj_;
  Fortran::frontend::CompilerInstance &ci_;
};

class ParseTreeClassStructureDumper {
public:
  explicit ParseTreeClassStructureDumper(llvm::raw_ostream &out) : out_(out) {}

  template <typename T> std::string GetClassName(const T &x) {
    return typeid(T).name();
  }

  template <typename T> bool Pre(const T &x) {
    out_ << std::string(indent_ * 3, ' ') << GetClassName(x) << "\n";

    ++indent_;

    return true;
  }

  template <typename T> void Post(const T &x) { --indent_; }

private:
  int indent_{0};
  llvm::raw_ostream &out_;
};

// inline llvm::raw_ostream &DumpTreeRob(
//     llvm::raw_ostream &out, Fortran::frontend::CompilerInstance &ci) {
//   llvm::json::Object obj;
//
//   RobDump dumper{obj, ci};
//   Walk(ci.getParsing().parseTree(), dumper);
//
//   llvm::json::OStream json_out{out, 3};
//
//   json_out.value(llvm::json::Value(std::move(obj)));
//
//   return out;
// }

inline llvm::raw_ostream &DumpTreeRob(
    llvm::raw_ostream &out, Fortran::frontend::CompilerInstance &ci) {
  ParseTreeClassStructureDumper dumper{out};
  Walk(ci.getParsing().parseTree(), dumper);

  return out;
}

} // namespace Fortran::parser

#endif // FORTRAN_PARSER_ROB_DUMP_H_
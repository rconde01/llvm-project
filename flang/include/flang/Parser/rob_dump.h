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
#include "llvm/Support/raw_ostream.h"
#include <string>
#include <type_traits>

namespace Fortran::parser {
class RobDump {
public:
  explicit RobDump(
      llvm::raw_ostream &out, Fortran::frontend::CompilerInstance &ci)
      : out_(out), ci_{ci} {}

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

    out_ << fortran;

    return true;
  }

  template <typename T> void Post(const T &x) {}

private:
  int indent_{0};
  llvm::raw_ostream &out_;
  Fortran::frontend::CompilerInstance &ci_;
  bool emptyline_{false};
};

inline llvm::raw_ostream &DumpTreeRob(
    llvm::raw_ostream &out, Fortran::frontend::CompilerInstance &ci) {
  RobDump dumper{out, ci};
  Walk(ci.getParsing().parseTree(), dumper);
  return out;
}

} // namespace Fortran::parser

#endif // FORTRAN_PARSER_ROB_DUMP_H_
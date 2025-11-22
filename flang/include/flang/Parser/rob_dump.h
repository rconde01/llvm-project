#ifndef FORTRAN_PARSER_ROB_DUMP_H_
#define FORTRAN_PARSER_ROB_DUMP_H_

#include "flang/Common/idioms.h"
#include "flang/Common/indirection.h"
#include "flang/Parser/format-specification.h"
#include "flang/Parser/parse-tree-visitor.h"
#include "flang/Parser/parse-tree.h"
#include "flang/Parser/tools.h"
#include "flang/Parser/unparse.h"
#include "flang/Support/Fortran.h"
#include "llvm/Frontend/OpenMP/OMP.h"
#include "llvm/Support/raw_ostream.h"
#include <string>
#include <type_traits>

namespace Fortran::parser {
class RobDump {
public:
  explicit RobDump(llvm::raw_ostream &out,
                   const AnalyzedObjectsAsFortran *asFortran = nullptr)
      : out_(out), asFortran_{asFortran} {}

  template <typename T>
  std::string AsFortran(const T &x) {
    return "Unhandled";
  }

  template <typename T>
  bool Pre(const T &x) {}

  template <typename T>
  void Post(const T &x) {}

private:
  int indent_{0};
  llvm::raw_ostream &out_;
  const AnalyzedObjectsAsFortran *const asFortran_;
  bool emptyline_{false};
};

template <typename T>
llvm::raw_ostream &
DumpTreeRob(llvm::raw_ostream &out, const T &x,
            const AnalyzedObjectsAsFortran *asFortran = nullptr) {
  RobDump dumper{out, asFortran};
  Walk(x, dumper);
  return out;
}

} // namespace Fortran::parser

#endif // FORTRAN_PARSER_ROB_DUMP_H_
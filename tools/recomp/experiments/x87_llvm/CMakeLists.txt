cmake_minimum_required(VERSION 3.20)
project(recomp_x87_llvm C CXX)
find_package(LLVM REQUIRED CONFIG)
if(NOT LLVM_VERSION_MAJOR EQUAL 22)
  message(FATAL_ERROR "This experiment requires LLVM 22")
endif()
list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
include(AddLLVM)
include_directories(SYSTEM ${LLVM_INCLUDE_DIRS})
add_definitions(${LLVM_DEFINITIONS})
set(CMAKE_CXX_STANDARD 17)
add_llvm_pass_plugin(RecompX87 PARTIAL_SOURCES_INTENDED stack_pass.cpp)
set(TOOLS "${LLVM_TOOLS_BINARY_DIR}")
set(OUT "${CMAKE_BINARY_DIR}")
add_custom_command(OUTPUT "${OUT}/helpers.bc"
  COMMAND "${TOOLS}/clang" -O1 -ffp-contract=off -emit-llvm -c
          -I "${KIT_RUNTIME}" "${CMAKE_CURRENT_SOURCE_DIR}/helpers.c" -o "${OUT}/helpers.bc"
  DEPENDS helpers.c "${KIT_RUNTIME}/x86.h" VERBATIM)
add_custom_command(OUTPUT "${OUT}/lifted.ll"
  COMMAND "${TOOLS}/opt" -load-pass-plugin=$<TARGET_FILE:RecompX87>
          -passes=recomp-x87-stack -verify-each -S "${OUT}/input.ll" -o "${OUT}/lifted.ll"
  DEPENDS RecompX87 "${OUT}/input.ll" VERBATIM)
add_custom_command(OUTPUT "${OUT}/optimized.ll" "${OUT}/llvm.o" "${OUT}/llvm.s"
  COMMAND "${TOOLS}/llvm-link" "${OUT}/lifted.ll" "${OUT}/helpers.bc" -o "${OUT}/linked.bc"
  COMMAND "${TOOLS}/opt" "-passes=always-inline,default<O2>" -verify-each -S
          "${OUT}/linked.bc" -o "${OUT}/optimized.ll"
  COMMAND "${TOOLS}/clang" -O2 -ffp-contract=off -c "${OUT}/optimized.ll" -o "${OUT}/llvm.o"
  COMMAND "${TOOLS}/clang" -O2 -ffp-contract=off -S "${OUT}/optimized.ll" -o "${OUT}/llvm.s"
  DEPENDS "${OUT}/lifted.ll" "${OUT}/helpers.bc" VERBATIM)
set_source_files_properties("${OUT}/llvm.o" PROPERTIES EXTERNAL_OBJECT TRUE GENERATED TRUE)
add_executable(x87_llvm "${OUT}/baseline.c" ../x87_locals/harness.c "${OUT}/llvm.o")
target_include_directories(x87_llvm PRIVATE "${KIT_RUNTIME}" "${OUT}")
target_compile_options(x87_llvm PRIVATE -O2 -ffp-contract=off)
if(NOT WIN32)
  target_link_libraries(x87_llvm PRIVATE m)
endif()

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
# State visibility depends on keeping memory adapters opaque to both emitters.
set_source_files_properties(access.c PROPERTIES COMPILE_OPTIONS "-fno-lto")
add_custom_command(OUTPUT "${OUT}/helpers.bc"
  COMMAND "${TOOLS}/clang" -O1 -ffp-contract=off -emit-llvm -c
          -I "${KIT_RUNTIME}" "${CMAKE_CURRENT_SOURCE_DIR}/helpers.c" -o "${OUT}/helpers.bc"
  DEPENDS helpers.c access.h "${KIT_RUNTIME}/x86.h" VERBATIM)
add_custom_command(OUTPUT "${OUT}/ssa.ll"
  COMMAND "${TOOLS}/opt" -load-pass-plugin=$<TARGET_FILE:RecompX87>
          -passes=recomp-x87-analyze,recomp-x87-ssa -verify-each -S "${OUT}/input.ll" -o "${OUT}/ssa.ll"
  DEPENDS RecompX87 "${OUT}/input.ll" VERBATIM)
add_custom_command(OUTPUT "${OUT}/effects.ll"
  COMMAND "${TOOLS}/opt" -load-pass-plugin=$<TARGET_FILE:RecompX87>
          -passes=recomp-x87-effects -verify-each -S "${OUT}/ssa.ll" -o "${OUT}/effects.ll"
  DEPENDS RecompX87 "${OUT}/ssa.ll" VERBATIM)
add_custom_command(OUTPUT "${OUT}/lifted.ll"
  COMMAND "${TOOLS}/opt" -load-pass-plugin=$<TARGET_FILE:RecompX87>
          -passes=recomp-x87-materialize -verify-each -S "${OUT}/effects.ll" -o "${OUT}/lifted.ll"
  DEPENDS RecompX87 "${OUT}/effects.ll" VERBATIM)
add_custom_command(OUTPUT "${OUT}/optimized.ll" "${OUT}/llvm.o" "${OUT}/llvm.s"
  COMMAND "${TOOLS}/llvm-link" "${OUT}/lifted.ll" "${OUT}/helpers.bc" -o "${OUT}/linked.bc"
  COMMAND "${TOOLS}/opt" "-passes=always-inline,default<O2>" -verify-each -S
          "${OUT}/linked.bc" -o "${OUT}/optimized.ll"
  COMMAND "${TOOLS}/clang" -O2 -ffp-contract=off -c "${OUT}/optimized.ll" -o "${OUT}/llvm.o"
  COMMAND "${TOOLS}/clang" -O2 -ffp-contract=off -S "${OUT}/optimized.ll" -o "${OUT}/llvm.s"
  DEPENDS "${OUT}/lifted.ll" "${OUT}/helpers.bc" VERBATIM)
set_source_files_properties("${OUT}/llvm.o" PROPERTIES EXTERNAL_OBJECT TRUE GENERATED TRUE)
# Own the generated object in one target: sharing a custom-command output
# directly between two executables lets Make run the optimizer twice in parallel.
add_library(x87_ir STATIC "${OUT}/llvm.o")
set_target_properties(x87_ir PROPERTIES LINKER_LANGUAGE C)
add_executable(x87_llvm boundaries.c access.c "${OUT}/baseline.c" ../../corpus/fragments/harness.c)
target_link_libraries(x87_llvm PRIVATE x87_ir)
target_include_directories(x87_llvm PRIVATE "${KIT_RUNTIME}" "${OUT}" "${CMAKE_CURRENT_SOURCE_DIR}")
target_compile_definitions(x87_llvm PRIVATE RK_MEMORY_TEST=1)
target_compile_options(x87_llvm PRIVATE -O2 -ffp-contract=off)
if(NOT WIN32)
  target_link_libraries(x87_llvm PRIVATE m)
endif()

if(FUNCTION_TEST)
  add_executable(x87_function "${OUT}/function/baseline.c" ../../corpus/fragments/harness.c
                 boundaries.c access.c)
  target_link_libraries(x87_function PRIVATE x87_ir)
  target_include_directories(x87_function PRIVATE "${KIT_RUNTIME}" "${OUT}/function" "${CMAKE_CURRENT_SOURCE_DIR}")
  target_compile_definitions(x87_function PRIVATE RK_FUNCTION_TEST=1 RK_MEMORY_TEST=1)
  target_compile_options(x87_function PRIVATE -O2 -ffp-contract=off)
  if(NOT WIN32)
    target_link_libraries(x87_function PRIVATE m)
  endif()
endif()

# Headline code-generation comparison: production C emitter vs direct LLVM,
# with equal normal-exit contracts and no artificial FNSTSW/access observers.
function(add_direct_probe target directory)
  add_executable(${target} "${directory}/baseline.c" ../../corpus/fragments/harness.c boundaries.c access.c)
  target_link_libraries(${target} PRIVATE x87_ir)
  target_include_directories(${target} PRIVATE "${KIT_RUNTIME}" "${directory}" "${CMAKE_CURRENT_SOURCE_DIR}")
  target_compile_definitions(${target} PRIVATE RK_DIRECT_TEST=1)
  target_compile_options(${target} PRIVATE -O2 -ffp-contract=off)
  if(NOT WIN32)
    target_link_libraries(${target} PRIVATE m)
  endif()
  add_custom_command(OUTPUT "${directory}/baseline.s"
    COMMAND "${TOOLS}/clang" -O2 -ffp-contract=off -S -I "${KIT_RUNTIME}"
            -I "${CMAKE_CURRENT_SOURCE_DIR}" "${directory}/baseline.c" -o "${directory}/baseline.s"
    DEPENDS "${directory}/baseline.c" "${KIT_RUNTIME}/x86.h" access.h VERBATIM)
  add_custom_target(${target}_assembly ALL DEPENDS "${directory}/baseline.s")
endfunction()
add_direct_probe(x87_direct "${OUT}/direct")
if(DIRECT_FUNCTION_TEST)
  add_direct_probe(x87_function_direct "${OUT}/function-direct")
  target_compile_definitions(x87_function_direct PRIVATE RK_FUNCTION_TEST=1)
endif()

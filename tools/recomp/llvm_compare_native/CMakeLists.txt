cmake_minimum_required(VERSION 3.24)
project(recomp_llvm_compare C CXX)
find_package(LLVM REQUIRED CONFIG)
if(NOT LLVM_VERSION_MAJOR EQUAL 22)
  message(FATAL_ERROR "This bounded comparison requires LLVM 22")
endif()
list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
include(AddLLVM)
include_directories(SYSTEM ${LLVM_INCLUDE_DIRS})
add_definitions(${LLVM_DEFINITIONS})
set(CMAKE_CXX_STANDARD 17)
get_filename_component(KIT "${CMAKE_CURRENT_SOURCE_DIR}/../../.." ABSOLUTE)
set(EXPERIMENT "${CMAKE_CURRENT_SOURCE_DIR}/../experiments/x87_llvm")
set(TOOLS "${LLVM_TOOLS_BINARY_DIR}")
set(TIME "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/time_command.py")
add_llvm_pass_plugin(RecompX87 PARTIAL_SOURCES_INTENDED "${EXPERIMENT}/stack_pass.cpp")
include("${CMAKE_BINARY_DIR}/settings.cmake")
foreach(LEAF IN LISTS LEAVES)
  set(D "${CMAKE_BINARY_DIR}/${LEAF}")
  include("${D}/settings.cmake")
  # Freeze both emitted C and production build arguments. Only the export name
  # changes, so these objects cannot replace a production fn_ADDR by accident.
  foreach(VARIANT IN ITEMS native llvm)
    if(VARIANT STREQUAL "native")
      set(CC "${PRODUCTION_CC}")
    else()
      set(CC "${TOOLS}/clang")
    endif()
    add_custom_command(OUTPUT "${D}/c_${VARIANT}.o" "${D}/c_${VARIANT}.s"
      COMMAND ${TIME} "${D}/time-c_${VARIANT}.json" "${CC}" ${PRODUCTION_FLAGS}
              -DCOMPARE_SYMBOL=compare_c_${VARIANT} -c "${D}/baseline.c" -o "${D}/c_${VARIANT}.o"
      COMMAND "${CC}" ${PRODUCTION_FLAGS} -DCOMPARE_SYMBOL=compare_c_${VARIANT}
              -S "${D}/baseline.c" -o "${D}/c_${VARIANT}.s"
      WORKING_DIRECTORY "${PRODUCTION_CWD}"
      DEPENDS "${D}/baseline.c" "${D}/body.h" "${D}/settings.cmake"
              "${KIT}/runtime/x86.h" VERBATIM)
  endforeach()
  add_custom_command(OUTPUT "${D}/helpers.bc"
    COMMAND ${TIME} "${D}/time-helpers.json" "${TOOLS}/clang" ${PRODUCTION_FLAGS}
            -O1 -emit-llvm -c "${EXPERIMENT}/helpers.c" -o "${D}/helpers.bc"
    DEPENDS "${EXPERIMENT}/helpers.c" "${EXPERIMENT}/access.h" "${KIT}/runtime/x86.h"
            "${D}/settings.cmake" VERBATIM)
  add_custom_command(OUTPUT "${D}/ssa.ll" "${D}/effects.ll" "${D}/lifted.ll"
    COMMAND ${TIME} "${D}/time-ssa.json" "${TOOLS}/opt" -load-pass-plugin=$<TARGET_FILE:RecompX87>
            -passes=recomp-x87-analyze,recomp-x87-ssa -verify-each -S "${D}/input.ll" -o "${D}/ssa.ll"
    COMMAND ${TIME} "${D}/time-effects.json" "${TOOLS}/opt" -load-pass-plugin=$<TARGET_FILE:RecompX87>
            -passes=recomp-x87-effects -verify-each -S "${D}/ssa.ll" -o "${D}/effects.ll"
    COMMAND ${TIME} "${D}/time-materialize.json" "${TOOLS}/opt" -load-pass-plugin=$<TARGET_FILE:RecompX87>
            -passes=recomp-x87-materialize -verify-each -S "${D}/effects.ll" -o "${D}/lifted.ll"
    DEPENDS RecompX87 "${D}/input.ll" VERBATIM)
  add_custom_command(OUTPUT "${D}/optimized.ll" "${D}/llvm.o" "${D}/llvm.s"
    COMMAND ${TIME} "${D}/time-link.json" "${TOOLS}/llvm-link" "${D}/lifted.ll" "${D}/helpers.bc" -o "${D}/linked.bc"
    COMMAND ${TIME} "${D}/time-o2.json" "${TOOLS}/opt" "-passes=always-inline,default<O2>"
            -verify-each -S "${D}/linked.bc" -o "${D}/optimized.ll"
    COMMAND ${TIME} "${D}/time-backend.json" "${TOOLS}/clang" ${PRODUCTION_FLAGS}
            -Wno-unused-command-line-argument -c "${D}/optimized.ll" -o "${D}/llvm.o"
    COMMAND "${TOOLS}/clang" ${PRODUCTION_FLAGS} -Wno-unused-command-line-argument
            -S "${D}/optimized.ll" -o "${D}/llvm.s"
    DEPENDS "${D}/lifted.ll" "${D}/helpers.bc" "${D}/settings.cmake" VERBATIM)
  set_source_files_properties("${D}/c_native.o" "${D}/c_llvm.o" "${D}/llvm.o" PROPERTIES EXTERNAL_OBJECT TRUE GENERATED TRUE)
  add_executable(replay_${LEAF} "${D}/c_native.o" "${D}/c_llvm.o" "${D}/llvm.o"
    "${EXPERIMENT}/boundaries.c" "${EXPERIMENT}/access.c" "${EXPERIMENT}/../x87_locals/harness.c")
  target_include_directories(replay_${LEAF} PRIVATE "${D}" "${EXPERIMENT}" "${KIT}/runtime")
  target_compile_options(replay_${LEAF} PRIVATE ${PRODUCTION_FLAGS})
  target_compile_definitions(replay_${LEAF} PRIVATE RK_FUNCTION_TEST=1 RK_DIRECT_TEST=1 RK_CODEGEN_TEST=1)
  set_target_properties(replay_${LEAF} PROPERTIES OUTPUT_NAME replay RUNTIME_OUTPUT_DIRECTORY "${D}")
  if(NOT WIN32)
    target_link_libraries(replay_${LEAF} PRIVATE m)
  endif()
endforeach()

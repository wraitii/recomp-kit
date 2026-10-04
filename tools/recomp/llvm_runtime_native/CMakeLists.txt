cmake_minimum_required(VERSION 3.24)
project(recomp_llvm_runtime C)
find_package(LLVM REQUIRED CONFIG)
if(NOT LLVM_VERSION_MAJOR EQUAL 22)
  message(FATAL_ERROR "Bounded LLVM activation requires LLVM 22")
endif()
set(TOOLS "${LLVM_TOOLS_BINARY_DIR}")
include("${CMAKE_BINARY_DIR}/settings.cmake")
foreach(LEAF IN LISTS LEAVES)
  set(D "${CMAKE_BINARY_DIR}/${LEAF}")
  include("${COMPARISON}/${LEAF}/settings.cmake")
  # Export only the selected lifted function. Runtime helpers are private and
  # dead helpers/raw comparison functions disappear before object emission.
  add_custom_command(OUTPUT "${D}/runtime.o" "${D}/runtime.s" "${D}/runtime-optimized.ll"
    COMMAND "${TOOLS}/opt" -passes=internalize,always-inline,default<O2>
            -internalize-public-api-list=recomp_llvm_${LEAF} -verify-each -S
            "${D}/runtime.ll" -o "${D}/runtime-optimized.ll"
    COMMAND "${TOOLS}/clang" ${PRODUCTION_FLAGS} -Wno-unused-command-line-argument
            -c "${D}/runtime-optimized.ll" -o "${D}/runtime.o"
    COMMAND "${TOOLS}/clang" ${PRODUCTION_FLAGS} -Wno-unused-command-line-argument
            -S "${D}/runtime-optimized.ll" -o "${D}/runtime.s"
    DEPENDS "${D}/runtime.ll" "${COMPARISON}/${LEAF}/settings.cmake" VERBATIM)
  add_custom_target(runtime_${LEAF} ALL DEPENDS "${D}/runtime.o")
endforeach()

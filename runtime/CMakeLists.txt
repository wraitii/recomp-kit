# The guest runtime. Object libraries, so every source is linked into every
# consumer exactly as the shell scripts linked every object.
set(POP_RUNTIME_SOURCES
  memory.cpp loader.cpp imports.cpp cpu.cpp thunks.cpp seh.cpp profile.cpp kernel32.cpp kernel32_wide.cpp
  resources.cpp oleaut32.cpp mods_seam.cpp user32.cpp user32_wide.cpp user32_vcl.cpp user32_static.cpp
  misc.cpp misc_dlls.cpp comctl32.cpp gdi32.cpp gdi32_draw.cpp gdi32_text.cpp gdi32_truetype.cpp msimg32.cpp
  media_foundation.cpp layout.cpp interp.cpp discovery.cpp imagehlp.cpp call_trace.cpp)

# The faces the runtime's GDI draws Windows' own UI fonts with
# (third_party/fonts/opensans/README.md), embedded as byte arrays.
set(POP_BUNDLED_FONTS_DIR ${POP_ROOT}/third_party/fonts/opensans)
set(POP_BUNDLED_FONTS_CPP ${CMAKE_CURRENT_BINARY_DIR}/generated/bundled_fonts.cpp)
add_custom_command(
  OUTPUT ${POP_BUNDLED_FONTS_CPP}
  COMMAND ${CMAKE_COMMAND} -DOUT=${POP_BUNDLED_FONTS_CPP} -DNAMESPACE=gdi
          "-DENTRIES=bundled_sans_regular=${POP_BUNDLED_FONTS_DIR}/OpenSans-Regular.ttf|bundled_sans_semibold=${POP_BUNDLED_FONTS_DIR}/OpenSans-Semibold.ttf"
          -P ${POP_ROOT}/cmake/EmbedFiles.cmake
  DEPENDS ${POP_ROOT}/cmake/EmbedFiles.cmake
          ${POP_BUNDLED_FONTS_DIR}/OpenSans-Regular.ttf
          ${POP_BUNDLED_FONTS_DIR}/OpenSans-Semibold.ttf
  COMMENT "Embedding the bundled fonts"
  VERBATIM)
list(APPEND POP_RUNTIME_SOURCES ${POP_BUNDLED_FONTS_CPP})

add_library(recomp_runtime OBJECT ${POP_RUNTIME_SOURCES})
target_include_directories(recomp_runtime PUBLIC ${POP_ROOT} ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(recomp_runtime PUBLIC recomp_platform)
target_compile_options(recomp_runtime PRIVATE ${POP_WARN_HOST})
pop_optimize(recomp_runtime 2)

# POPM_TESTING compiles the test-only entry points (loader reset, owner
# counter seam, scheduler latch reset). None exists in a real build.
if(NOT IOS AND NOT ANDROID)
add_library(recomp_runtime_testing OBJECT ${POP_RUNTIME_SOURCES})
target_include_directories(recomp_runtime_testing PUBLIC ${POP_ROOT} ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(recomp_runtime_testing PUBLIC recomp_platform)
target_compile_definitions(recomp_runtime_testing PUBLIC POPM_TESTING=1)
target_compile_options(recomp_runtime_testing PRIVATE ${POP_WARN_HOST})
pop_optimize(recomp_runtime_testing 1)
endif()

# The interpreter for heap code, on its own: it needs guest memory and
# recomp_call, which the test provides.
if(NOT IOS AND NOT ANDROID)
set(ENTRY_FIXTURE_DIR ${CMAKE_CURRENT_BINARY_DIR}/generated/entry-fixture)
set(ENTRY_FIXTURE_SOURCES
  ${ENTRY_FIXTURE_DIR}/chunk_0d000000_14.c
  ${ENTRY_FIXTURE_DIR}/chunk_entries_0d000000.c
  ${ENTRY_FIXTURE_DIR}/fixture_table.c)
add_custom_command(
  OUTPUT ${ENTRY_FIXTURE_SOURCES} ${ENTRY_FIXTURE_DIR}/body.h
  COMMAND ${Python3_EXECUTABLE} ${POP_ROOT}/tools/recomp/tests/gen_entry_fixture.py ${ENTRY_FIXTURE_DIR}
  DEPENDS ${POP_ROOT}/tools/recomp/tests/gen_entry_fixture.py ${POP_ROOT}/tools/recomp/translate.py
  COMMENT "Generating stable-entry dispatch fixture"
  VERBATIM)
foreach(entry_test entry_dispatch_tests entry_dispatch_nohooks_tests)
  add_executable(${entry_test} tests/entry_dispatch_tests.c ${ENTRY_FIXTURE_SOURCES})
  target_include_directories(${entry_test} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR} ${ENTRY_FIXTURE_DIR})
  target_compile_options(${entry_test} PRIVATE ${POP_WARN_HOST})
  pop_optimize(${entry_test} 2)
  pop_test_binary(${entry_test})
  add_test(NAME ${entry_test} COMMAND ${entry_test})
  set_tests_properties(${entry_test} PROPERTIES LABELS nogame)
endforeach()
target_compile_definitions(entry_dispatch_nohooks_tests PRIVATE RECOMP_NO_HOOKS=1)
add_custom_target(dispatch_tests DEPENDS entry_dispatch_tests entry_dispatch_nohooks_tests)

# The env-gated guest call tracer, against the game-free fixture. call_trace
# lives in recomp_runtime with weak table fallbacks; the fixture's strong tables
# override them and provide real entry dispatch to hook.
add_executable(call_trace_tests tests/call_trace_tests.cpp ${ENTRY_FIXTURE_SOURCES} tests/stub_recomp_call.cpp)
target_include_directories(call_trace_tests PRIVATE ${CMAKE_CURRENT_SOURCE_DIR} ${ENTRY_FIXTURE_DIR})
target_link_libraries(call_trace_tests PRIVATE recomp_platform recomp_runtime)
target_compile_options(call_trace_tests PRIVATE ${POP_WARN_HOST})
pop_optimize(call_trace_tests 1)
pop_test_binary(call_trace_tests)
add_test(NAME call_trace_tests COMMAND call_trace_tests WORKING_DIRECTORY ${POP_ROOT})
set_tests_properties(call_trace_tests PROPERTIES LABELS game TIMEOUT 120)

add_executable(interp_tests tests/interp_tests.cpp interp.cpp)
target_include_directories(interp_tests PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_options(interp_tests PRIVATE ${POP_WARN_HOST})
pop_test_binary(interp_tests)
add_test(NAME interp_tests COMMAND interp_tests WORKING_DIRECTORY ${POP_ROOT})
# TIMEOUT because these checks assert that a guest routine cannot run
# forever: a regression in them is a hang, not a failure, and a hung run
# reports nothing at all. The suite takes under a second.
set_tests_properties(interp_tests PROPERTIES LABELS nogame TIMEOUT 120)
endif()

# Linked by the hosts, the fixture and the mod tests, not by the unit tests.
add_library(recomp_snapshot OBJECT snapshot.cpp)
target_link_libraries(recomp_snapshot PUBLIC recomp_runtime)
target_compile_options(recomp_snapshot PRIVATE ${POP_WARN_HOST})
pop_optimize(recomp_snapshot 2)

# The suite spawns itself for the abort check; portable since sub-project 3.
if(NOT IOS AND NOT ANDROID)
add_executable(seh_tests tests/seh_tests.cpp)
target_link_libraries(seh_tests PRIVATE recomp_platform recomp_runtime_testing)
target_compile_options(seh_tests PRIVATE ${POP_WARN_HOST})
pop_optimize(seh_tests 1)
pop_test_binary(seh_tests)
add_test(NAME seh_tests COMMAND seh_tests WORKING_DIRECTORY ${POP_ROOT})
set_tests_properties(seh_tests PROPERTIES LABELS nogame ENVIRONMENT RECOMP_PROFILE=1)

add_executable(runtime_tests tests/runtime_tests.cpp tests/stub_recomp_call.cpp)
target_link_libraries(runtime_tests PRIVATE recomp_platform recomp_runtime recomp_dx)
target_compile_options(runtime_tests PRIVATE ${POP_WARN_HOST})
pop_optimize(runtime_tests 1)
pop_test_binary(runtime_tests)
add_test(NAME runtime_tests COMMAND runtime_tests WORKING_DIRECTORY ${POP_ROOT})
# TIMEOUT for the reason interp_tests has one: this suite also runs guest
# code through the interpreter. It takes two seconds when that returns.
set_tests_properties(runtime_tests PROPERTIES LABELS game TIMEOUT 600
                     ENVIRONMENT RECOMP_PYTHON=${Python3_EXECUTABLE})
add_executable(gdi_tests tests/gdi_tests.cpp tests/stub_recomp_call.cpp)
target_link_libraries(gdi_tests PRIVATE recomp_platform recomp_runtime)
target_compile_options(gdi_tests PRIVATE ${POP_WARN_HOST})
pop_optimize(gdi_tests 1)
pop_test_binary(gdi_tests)
add_test(NAME gdi_tests COMMAND gdi_tests WORKING_DIRECTORY ${POP_ROOT})
set_tests_properties(gdi_tests PROPERTIES LABELS nogame)
add_test(NAME gdi_model_tests COMMAND gdi_tests model WORKING_DIRECTORY ${POP_ROOT})
set_tests_properties(gdi_model_tests PROPERTIES LABELS nogame)
add_test(NAME gdi_draw_tests COMMAND gdi_tests draw WORKING_DIRECTORY ${POP_ROOT})
set_tests_properties(gdi_draw_tests PROPERTIES LABELS nogame)

endif()

if(POP_REAL_GEN AND NOT IOS AND NOT ANDROID)
  # The headless parity fixture: null host, real generated code, core mods installed.
  add_executable(pop_fixture fixture.cpp)
  target_link_libraries(pop_fixture PRIVATE recomp_platform recomp_runtime recomp_snapshot recomp_dx_null recomp_mods lua)
  target_compile_options(pop_fixture PRIVATE ${POP_WARN_HOST})
  pop_optimize(pop_fixture 1)
  pop_link_gen(pop_fixture)
  if(TARGET core_mods)
    add_dependencies(pop_fixture core_mods)
  endif()

  # CALL_FN takes an address token (without 0x), not a numeric expression.
  # Select the first generated entry; define the suite only when one exists.
  set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${POP_GEN_DIR}/funcs.h)
  file(STRINGS ${POP_GEN_DIR}/funcs.h PROFILE_FIRST_INDEX
    REGEX "^#define FIDX_[0-9a-f]+ 0u$" LIMIT_COUNT 1)
  if(PROFILE_FIRST_INDEX)
    add_executable(profile_tests tests/profile_tests.cpp)
    add_dependencies(dispatch_tests profile_tests)
    string(REGEX REPLACE "^#define FIDX_([0-9a-f]+) 0u$" "\\1"
      PROFILE_TARGET_TOKEN "${PROFILE_FIRST_INDEX}")
    target_compile_definitions(profile_tests PRIVATE RECOMP_PROFILE_TARGET=${PROFILE_TARGET_TOKEN})
    target_link_libraries(profile_tests PRIVATE recomp_platform recomp_runtime recomp_dx_null)
    target_compile_options(profile_tests PRIVATE ${POP_WARN_HOST})
    pop_optimize(profile_tests 1)
    pop_link_gen(profile_tests)
    pop_test_binary(profile_tests)
    add_test(NAME profile_tests_disabled COMMAND profile_tests disabled WORKING_DIRECTORY ${POP_ROOT})
    set_tests_properties(profile_tests_disabled PROPERTIES LABELS game ENVIRONMENT RECOMP_PROFILE=0)
    add_test(NAME profile_tests_enabled COMMAND profile_tests enabled WORKING_DIRECTORY ${POP_ROOT})
    set_tests_properties(profile_tests_enabled PROPERTIES LABELS game ENVIRONMENT RECOMP_PROFILE=1)
  else()
    message(STATUS "profile_tests: the translation has no FIDX entry; suite not defined")
  endif()
endif()

if(POP_TRACE_DIR AND NOT IOS AND NOT ANDROID)
  # Caller-supplied trace wrappers over a snapshot of gen/: the override header
  # is force-included ahead of funcs.h so every FN_<addr> macro is already set.
  file(GLOB POP_TRACE_SOURCES CONFIGURE_DEPENDS ${POP_TRACE_DIR}/gen/chunk_*.c ${POP_TRACE_DIR}/gen/table.c)
  add_library(recomp_gen_trace OBJECT ${POP_TRACE_SOURCES} ${POP_TRACE_DIR}/trace_wrappers.c)
  target_include_directories(recomp_gen_trace PRIVATE ${POP_TRACE_DIR} ${POP_TRACE_DIR}/gen ${POP_ROOT} ${POP_ROOT}/runtime)
  target_compile_options(recomp_gen_trace PRIVATE ${POP_WARN_GEN} -include ${POP_TRACE_DIR}/trace_override.h)
  pop_optimize(recomp_gen_trace 1)
  add_executable(pop_fixture_trace fixture.cpp)
  target_link_libraries(pop_fixture_trace PRIVATE recomp_platform recomp_runtime recomp_snapshot recomp_dx_null recomp_mods lua recomp_gen_trace)
  target_include_directories(pop_fixture_trace PRIVATE ${POP_TRACE_DIR}/gen)
  target_compile_options(pop_fixture_trace PRIVATE ${POP_WARN_HOST})
  pop_optimize(pop_fixture_trace 1)
endif()

# The translated game lives only in the developer's build/recomp/gen; the
# kit never tracks generated code (spec section 11). Generated C is platform
# independent, so every preset (macOS, iOS) compiles the same directory.
set(RECOMP_GEN_DIR ${RECOMP_BUILD_ROOT}/recomp/gen)
if(RECOMP_TRANSLATE STREQUAL "STUB")
  # A link-only translation for builds without game code (CI).
  set(RECOMP_GEN_DIR ${CMAKE_BINARY_DIR}/stub-gen)
  execute_process(
    COMMAND ${Python3_EXECUTABLE} ${RECOMP_ROOT}/tools/gen_stub_translation.py --out ${RECOMP_GEN_DIR}
    RESULT_VARIABLE RECOMP_STUB_RESULT)
  if(NOT RECOMP_STUB_RESULT EQUAL 0)
    message(FATAL_ERROR "tools/gen_stub_translation.py failed")
  endif()
endif()
set(RECOMP_HAVE_GEN OFF)
# RECOMP_REAL_GEN: the translation is the game's, not the link-only stub. Tests
# that call into generated functions by address are defined only then.
set(RECOMP_REAL_GEN OFF)
if(RECOMP_TRANSLATE STREQUAL "OFF")
  message(STATUS "RECOMP_TRANSLATE=OFF: targets that need the generated code are not defined")
elseif(EXISTS ${RECOMP_GEN_DIR}/table.c)
  set(RECOMP_HAVE_GEN ON)
  if(NOT RECOMP_TRANSLATE STREQUAL "STUB")
    set(RECOMP_REAL_GEN ON)
  endif()
  message(STATUS "Translation: ${RECOMP_GEN_DIR}")
elseif(RECOMP_TRANSLATE STREQUAL "ON")
  message(FATAL_ERROR "RECOMP_TRANSLATE=ON but no translation: run tools/build.py --regenerate")
else()
  message(STATUS "No translation found: hosts and game-backed tests are not defined")
endif()

# New translations isolate overrides in table.c. Keep old generated trees usable
# until their next regeneration; those still need the legacy target-wide define.
function(recomp_translation_overrides target dir header)
  if(NOT header)
    return()
  endif()
  if(NOT EXISTS "${header}")
    message(FATAL_ERROR "RECOMP_OVERRIDE_HEADER does not exist: ${header}")
  endif()
  get_filename_component(override_dir "${header}" DIRECTORY)
  if(EXISTS "${dir}/body.h")
    set_property(SOURCE "${dir}/table.c" APPEND PROPERTY
      COMPILE_DEFINITIONS RECOMP_OVERRIDE_HEADER="${header}")
    set_property(SOURCE "${dir}/table.c" APPEND PROPERTY INCLUDE_DIRECTORIES "${override_dir}")
  else()
    target_compile_definitions(${target} PRIVATE RECOMP_OVERRIDE_HEADER="${header}")
    target_include_directories(${target} PRIVATE "${override_dir}")
  endif()
endfunction()

# Submit isolated giants first so their compile can overlap the small chunks.
function(recomp_translation_sources result dir)
  file(GLOB sources CONFIGURE_DEPENDS ${dir}/chunk_*.c ${dir}/table.c)
  set(giants ${sources})
  list(FILTER giants INCLUDE REGEX "/chunk_fn_[0-9a-f]+\\.c$")
  if(giants)
    list(REMOVE_ITEM sources ${giants})
  endif()
  set(${result} ${giants} ${sources} PARENT_SCOPE)
endfunction()

set(RECOMP_EFFECTIVE_OVERRIDE "${RECOMP_OVERRIDE_HEADER}")
if(RECOMP_NATIVE_HEADER AND NOT RECOMP_TRANSLATE STREQUAL "STUB")
  set(RECOMP_EFFECTIVE_OVERRIDE "${RECOMP_NATIVE_HEADER}")
endif()

if(RECOMP_HAVE_GEN)
  recomp_translation_sources(RECOMP_GEN_SOURCES "${RECOMP_GEN_DIR}")
  add_library(recomp_gen STATIC ${RECOMP_GEN_SOURCES})
  set_target_properties(recomp_gen PROPERTIES
    ARCHIVE_OUTPUT_DIRECTORY ${RECOMP_ARCHIVE_DIR} OUTPUT_NAME recomp_gen)
  # -I<gen> for x86.h beside the sources, -I<root> for the canonical copy,
  # -I<runtime> for intrinsics.h.
  target_include_directories(recomp_gen PRIVATE ${RECOMP_GEN_DIR} ${RECOMP_ROOT} ${RECOMP_ROOT}/runtime)
  target_include_directories(recomp_gen INTERFACE ${RECOMP_GEN_DIR})
  target_compile_options(recomp_gen PRIVATE ${RECOMP_WARN_GEN})
  # Function-scope locals gain nothing from lifetime markers; clang's call emission costs calls x locals.
  set_property(SOURCE ${RECOMP_GEN_SOURCES} APPEND PROPERTY COMPILE_OPTIONS
    "$<$<C_COMPILER_ID:Clang,AppleClang>:-Xclang;-disable-lifetime-markers>")
  recomp_translation_overrides(recomp_gen "${RECOMP_GEN_DIR}" "${RECOMP_EFFECTIVE_OVERRIDE}")
  recomp_optimize(recomp_gen 2)
  set(RECOMP_GEN_AUX_TARGETS "")
  foreach(key ${RECOMP_AUX_MODULES})
    set(dir "${RECOMP_GEN_DIR}/aux-${key}")
    if(NOT EXISTS "${dir}/table.c")
      message(FATAL_ERROR "Missing auxiliary translation: ${dir}")
    endif()
    set(aux_target "recomp_gen_${key}")
    recomp_translation_sources(aux_sources "${dir}")
    add_library(${aux_target} STATIC ${aux_sources})
    set_target_properties(${aux_target} PROPERTIES
      ARCHIVE_OUTPUT_DIRECTORY ${RECOMP_ARCHIVE_DIR} OUTPUT_NAME ${aux_target})
    target_include_directories(${aux_target} PRIVATE ${dir} ${RECOMP_GEN_DIR} ${RECOMP_ROOT} ${RECOMP_ROOT}/runtime)
    target_compile_options(${aux_target} PRIVATE ${RECOMP_WARN_GEN})
    recomp_optimize(${aux_target} 2)
    list(APPEND RECOMP_GEN_AUX_TARGETS ${aux_target})
  endforeach()
  # The game's native replacements (game.toml [translate] native).
  if(RECOMP_NATIVE_HEADER AND NOT RECOMP_TRANSLATE STREQUAL "STUB")
    target_sources(recomp_gen PRIVATE ${RECOMP_NATIVE_SOURCES})
  endif()
endif()

# The portable spelling of -Wl,-force_load: every generated object is kept
# whether or not anything references it, because the dispatch table is
# reached by address.
function(recomp_link_gen target)
  target_link_libraries(${target} PRIVATE "$<LINK_LIBRARY:WHOLE_ARCHIVE,recomp_gen>")
  foreach(aux_target ${RECOMP_GEN_AUX_TARGETS})
    target_link_libraries(${target} PRIVATE "$<LINK_LIBRARY:WHOLE_ARCHIVE,${aux_target}>")
  endforeach()
  target_include_directories(${target} PRIVATE ${RECOMP_GEN_DIR})
endfunction()

# The translated game lives only in the developer's build/recomp/gen; the
# kit never tracks generated code (spec section 11). Generated C is platform
# independent, so every preset (macOS, iOS) compiles the same directory.
set(POP_GEN_DIR ${POP_BUILD_ROOT}/recomp/gen)
if(POP_TRANSLATE STREQUAL "STUB")
  # A link-only translation for builds without game code (CI).
  set(POP_GEN_DIR ${CMAKE_BINARY_DIR}/stub-gen)
  execute_process(
    COMMAND ${Python3_EXECUTABLE} ${POP_ROOT}/tools/gen_stub_translation.py --out ${POP_GEN_DIR}
    RESULT_VARIABLE POP_STUB_RESULT)
  if(NOT POP_STUB_RESULT EQUAL 0)
    message(FATAL_ERROR "tools/gen_stub_translation.py failed")
  endif()
endif()
set(POP_HAVE_GEN OFF)
# POP_REAL_GEN: the translation is the game's, not the link-only stub. Tests
# that call into generated functions by address are defined only then.
set(POP_REAL_GEN OFF)
if(POP_TRANSLATE STREQUAL "OFF")
  message(STATUS "POP_TRANSLATE=OFF: targets that need the generated code are not defined")
elseif(EXISTS ${POP_GEN_DIR}/table.c)
  set(POP_HAVE_GEN ON)
  if(NOT POP_TRANSLATE STREQUAL "STUB")
    set(POP_REAL_GEN ON)
  endif()
  message(STATUS "Translation: ${POP_GEN_DIR}")
elseif(POP_TRANSLATE STREQUAL "ON")
  message(FATAL_ERROR "POP_TRANSLATE=ON but no translation: run tools/build.py --regenerate")
else()
  message(STATUS "No translation found: hosts and game-backed tests are not defined")
endif()

# New translations isolate overrides in table.c. Keep old generated trees usable
# until their next regeneration; those still need the legacy target-wide define.
function(pop_translation_overrides target dir header)
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
function(pop_translation_sources result dir)
  file(GLOB sources CONFIGURE_DEPENDS ${dir}/chunk_*.c ${dir}/table.c)
  set(giants ${sources})
  list(FILTER giants INCLUDE REGEX "/chunk_fn_[0-9a-f]+\\.c$")
  if(giants)
    list(REMOVE_ITEM sources ${giants})
  endif()
  set(${result} ${giants} ${sources} PARENT_SCOPE)
endfunction()

set(POP_EFFECTIVE_OVERRIDE "${RECOMP_OVERRIDE_HEADER}")
if(RECOMP_NATIVE_HEADER AND NOT POP_TRANSLATE STREQUAL "STUB")
  set(POP_EFFECTIVE_OVERRIDE "${RECOMP_NATIVE_HEADER}")
endif()

if(POP_HAVE_GEN)
  pop_translation_sources(POP_GEN_SOURCES "${POP_GEN_DIR}")
  add_library(recomp_gen STATIC ${POP_GEN_SOURCES})
  set_target_properties(recomp_gen PROPERTIES
    ARCHIVE_OUTPUT_DIRECTORY ${POP_ARCHIVE_DIR} OUTPUT_NAME recomp_gen)
  # -I<gen> for x86.h beside the sources, -I<root> for the canonical copy,
  # -I<runtime> for intrinsics.h: the same three the shell script passed.
  target_include_directories(recomp_gen PRIVATE ${POP_GEN_DIR} ${POP_ROOT} ${POP_ROOT}/runtime)
  target_include_directories(recomp_gen INTERFACE ${POP_GEN_DIR})
  target_compile_options(recomp_gen PRIVATE ${POP_WARN_GEN})
  pop_translation_overrides(recomp_gen "${POP_GEN_DIR}" "${POP_EFFECTIVE_OVERRIDE}")
  pop_optimize(recomp_gen 2)
  # Auxiliary modules (game.toml [modules.aux.<key>]) are translated into
  # gen/aux-<key>/ with their own funcs.h and prefixed tables, so each is its
  # own library; their table.c registers with the runtime's module registry.
  set(POP_GEN_AUX_TARGETS "")
  file(GLOB POP_GEN_AUX_DIRS CONFIGURE_DEPENDS LIST_DIRECTORIES true ${POP_GEN_DIR}/aux-*)
  foreach(dir ${POP_GEN_AUX_DIRS})
    if(NOT EXISTS ${dir}/table.c)
      continue()
    endif()
    get_filename_component(key ${dir} NAME)
    string(REPLACE "aux-" "recomp_gen_" aux_target ${key})
    pop_translation_sources(aux_sources "${dir}")
    add_library(${aux_target} STATIC ${aux_sources})
    set_target_properties(${aux_target} PROPERTIES
      ARCHIVE_OUTPUT_DIRECTORY ${POP_ARCHIVE_DIR} OUTPUT_NAME ${aux_target})
    target_include_directories(${aux_target} PRIVATE ${dir} ${POP_GEN_DIR} ${POP_ROOT} ${POP_ROOT}/runtime)
    pop_translation_overrides(${aux_target} "${dir}" "${POP_EFFECTIVE_OVERRIDE}")
    target_compile_options(${aux_target} PRIVATE ${POP_WARN_GEN})
    pop_optimize(${aux_target} 2)
    list(APPEND POP_GEN_AUX_TARGETS ${aux_target})
    message(STATUS "Auxiliary module translation: ${dir}")
  endforeach()
  # The game's native replacements (game.toml [translate] native).
  if(RECOMP_NATIVE_HEADER AND NOT POP_TRANSLATE STREQUAL "STUB")
    target_sources(recomp_gen PRIVATE ${RECOMP_NATIVE_SOURCES})
  endif()
endif()

# The portable spelling of -Wl,-force_load: every generated object is kept
# whether or not anything references it, because the dispatch table is
# reached by address.
function(pop_link_gen target)
  target_link_libraries(${target} PRIVATE "$<LINK_LIBRARY:WHOLE_ARCHIVE,recomp_gen>")
  foreach(aux_target ${POP_GEN_AUX_TARGETS})
    target_link_libraries(${target} PRIVATE "$<LINK_LIBRARY:WHOLE_ARCHIVE,${aux_target}>")
  endforeach()
  target_include_directories(${target} PRIVATE ${POP_GEN_DIR})
endfunction()

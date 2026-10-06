set(POP_DX_SOURCES
  com.cpp dx.cpp host_api.cpp display_stubs.cpp ddraw.cpp d3d.cpp d3d7.cpp d3d11.cpp dxgi.cpp d3dcompiler.cpp
  d3dx10.cpp dsound.cpp dshow.cpp mp3_source.cpp dinput.cpp dinput_joystick.cpp xinput.cpp qmixer.cpp
  mss32.cpp redbook.cpp avi.cpp waveout.cpp fmod.cpp soundlib.cpp galaxy_stub.cpp riff.cpp bink.cpp video_frame.cpp mf_media.cpp
  mf.cpp weanetr.cpp d3d9.cpp d3d9_raster.cpp d3d9_shader.cpp d3d9_shadergen.cpp host_d9_default.cpp
  d3dx9.cpp d3d8.cpp)

# A static archive lets ordinary hosts share miniz with the launcher without
# duplicating its object, and includes it for hosts that have no launcher.
add_library(recomp_frame_dump STATIC frame_dump.cpp $<TARGET_OBJECTS:recomp_miniz>)
target_link_libraries(recomp_frame_dump PUBLIC recomp_platform)
target_compile_options(recomp_frame_dump PRIVATE ${POP_WARN_HOST})
pop_optimize(recomp_frame_dump 1)

# Interface code is disposable build output; the pinned API header and handler
# choices are source inputs. Both renderer and null-host objects consume it.
set(RECOMP_D3D8_GENERATED_DIR "${POP_BUILD_ROOT}/d3d8-generated")
set(RECOMP_D3D8_COM_TABLES "${RECOMP_D3D8_GENERATED_DIR}/d3d8_interfaces.inc")
add_custom_command(OUTPUT "${RECOMP_D3D8_COM_TABLES}"
  COMMAND ${CMAKE_COMMAND} -E make_directory "${RECOMP_D3D8_GENERATED_DIR}"
  COMMAND ${Python3_EXECUTABLE} "${POP_ROOT}/tools/gen_com_interfaces.py"
          --reference "${POP_ROOT}/third_party/wine-d3d8/d3d8.h"
          --bindings "${CMAKE_CURRENT_SOURCE_DIR}/d3d8_bindings.json"
          --output "${RECOMP_D3D8_COM_TABLES}"
  DEPENDS "${POP_ROOT}/tools/gen_com_interfaces.py"
          "${POP_ROOT}/third_party/wine-d3d8/d3d8.h" "${CMAKE_CURRENT_SOURCE_DIR}/d3d8_bindings.json"
  VERBATIM)
set(RECOMP_D3D8_CONSTANTS "${RECOMP_D3D8_GENERATED_DIR}/d3d8_constants.inc")
add_custom_command(OUTPUT "${RECOMP_D3D8_CONSTANTS}"
  COMMAND ${CMAKE_COMMAND} -E make_directory "${RECOMP_D3D8_GENERATED_DIR}"
  COMMAND ${Python3_EXECUTABLE} "${POP_ROOT}/tools/gen_d3d8_constants.py"
          --reference "${POP_ROOT}/third_party/wine-d3d8/d3d8types.h"
                      "${POP_ROOT}/third_party/wine-d3d8/d3d8caps.h"
          --names "${CMAKE_CURRENT_SOURCE_DIR}/d3d8_constants.json"
          --output "${RECOMP_D3D8_CONSTANTS}"
  DEPENDS "${POP_ROOT}/tools/gen_d3d8_constants.py"
          "${POP_ROOT}/third_party/wine-d3d8/d3d8types.h"
          "${POP_ROOT}/third_party/wine-d3d8/d3d8caps.h"
          "${CMAKE_CURRENT_SOURCE_DIR}/d3d8_constants.json"
  VERBATIM)
add_custom_target(d3d8_com_tables DEPENDS "${RECOMP_D3D8_COM_TABLES}" "${RECOMP_D3D8_CONSTANTS}")

add_library(recomp_dx OBJECT ${POP_DX_SOURCES})
add_dependencies(recomp_dx d3d8_com_tables)
target_include_directories(recomp_dx PRIVATE "${RECOMP_D3D8_GENERATED_DIR}")
target_link_libraries(recomp_dx PUBLIC recomp_runtime recomp_frame_dump)
pop_link_video(recomp_dx)
target_compile_options(recomp_dx PRIVATE ${POP_WARN_HOST})
pop_optimize(recomp_dx 2)

# Optional Rust D3D8/wgpu host renderer, shipped in the kit as
# graphics/d3d8-wgpu. It is built on native macOS when RECOMP_D3D8_WGPU is ON
# (tools/build.py passes it from the environment) or when a game ships its own
# graphics/d3d8-wgpu, which then takes precedence. Every other build compiles
# no Rust and uses d3d8.cpp's no-renderer path only. The frameworks are what the
# probe binaries link through cargo's build scripts.
option(RECOMP_D3D8_WGPU "Build the kit's Rust D3D8/wgpu renderer" OFF)
set(RECOMP_D3D8_WGPU_DIR "")
if(EXISTS "${RECOMP_GAME_DIR}/graphics/d3d8-wgpu/Cargo.toml")
  set(RECOMP_D3D8_WGPU_DIR "${RECOMP_GAME_DIR}/graphics/d3d8-wgpu")
elseif(RECOMP_D3D8_WGPU)
  set(RECOMP_D3D8_WGPU_DIR "${CMAKE_CURRENT_LIST_DIR}/../graphics/d3d8-wgpu")
  get_filename_component(RECOMP_D3D8_WGPU_DIR "${RECOMP_D3D8_WGPU_DIR}" ABSOLUTE)
endif()
if(CMAKE_SYSTEM_NAME STREQUAL "Darwin" AND RECOMP_D3D8_WGPU_DIR AND EXISTS "${RECOMP_D3D8_WGPU_DIR}/Cargo.toml")
  find_program(CARGO_EXECUTABLE cargo)
  if(NOT CARGO_EXECUTABLE)
    message(FATAL_ERROR "the D3D8/wgpu renderer is enabled but cargo was not found")
  endif()
  # This host bridge has only been wired for native macOS builds. Cargo's
  # default target must not silently supply a host archive to a cross build.
  if(CMAKE_CROSSCOMPILING OR (CMAKE_OSX_ARCHITECTURES AND
      NOT CMAKE_OSX_ARCHITECTURES STREQUAL CMAKE_HOST_SYSTEM_PROCESSOR))
    message(FATAL_ERROR "D3D8/wgpu integration currently requires a native single-architecture macOS build")
  endif()
  find_program(CBINDGEN_EXECUTABLE cbindgen HINTS "${RECOMP_GAME_DIR}/build/tools/bin")
  if(NOT CBINDGEN_EXECUTABLE)
    message(FATAL_ERROR "D3D8 ABI generation requires cbindgen 0.29.4")
  endif()
  execute_process(COMMAND "${CBINDGEN_EXECUTABLE}" --version OUTPUT_VARIABLE RECOMP_CBINDGEN_VERSION OUTPUT_STRIP_TRAILING_WHITESPACE)
  if(NOT RECOMP_CBINDGEN_VERSION STREQUAL "cbindgen 0.29.4")
    message(FATAL_ERROR "D3D8 ABI generation requires cbindgen 0.29.4; found ${RECOMP_CBINDGEN_VERSION}")
  endif()
  file(GLOB_RECURSE RECOMP_D3D8_WGPU_SOURCES CONFIGURE_DEPENDS
    "${RECOMP_D3D8_WGPU_DIR}/src/*.rs")
  set(RECOMP_D3D8_WGPU_TARGET_DIR "${POP_BUILD_ROOT}/d3d8-wgpu/rust")
  set(RECOMP_D3D8_WGPU_LIB "${RECOMP_D3D8_WGPU_TARGET_DIR}/release/libd3d8_wgpu.a")
  add_custom_command(
    OUTPUT "${RECOMP_D3D8_WGPU_LIB}"
    COMMAND ${CMAKE_COMMAND} -E env "CARGO_TARGET_DIR=${RECOMP_D3D8_WGPU_TARGET_DIR}"
            "${CARGO_EXECUTABLE}" build --release --lib --locked --manifest-path "${RECOMP_D3D8_WGPU_DIR}/Cargo.toml"
    DEPENDS "${RECOMP_D3D8_WGPU_DIR}/Cargo.toml"
            "${RECOMP_D3D8_WGPU_DIR}/Cargo.lock"
            ${RECOMP_D3D8_WGPU_SOURCES}
    COMMENT "Building d3d8-wgpu Rust static library"
    VERBATIM)
  set(RECOMP_D3D8_ABI_HEADER "${RECOMP_D3D8_GENERATED_DIR}/d3d8_abi.h")
  add_custom_command(OUTPUT "${RECOMP_D3D8_ABI_HEADER}"
    COMMAND ${CMAKE_COMMAND} -E make_directory "${RECOMP_D3D8_GENERATED_DIR}"
    COMMAND "${CBINDGEN_EXECUTABLE}" --quiet --config "${RECOMP_D3D8_WGPU_DIR}/cbindgen.toml"
            --output "${RECOMP_D3D8_ABI_HEADER}" "${RECOMP_D3D8_WGPU_DIR}"
    DEPENDS "${RECOMP_D3D8_WGPU_DIR}/cbindgen.toml" "${RECOMP_D3D8_WGPU_DIR}/Cargo.toml"
            "${RECOMP_D3D8_WGPU_DIR}/Cargo.lock" ${RECOMP_D3D8_WGPU_SOURCES}
    VERBATIM)
  add_custom_target(d3d8_abi_header DEPENDS "${RECOMP_D3D8_ABI_HEADER}")
  add_dependencies(recomp_dx d3d8_abi_header)
  add_custom_target(d3d8_wgpu_rust DEPENDS "${RECOMP_D3D8_WGPU_LIB}")
  add_library(d3d8_wgpu STATIC IMPORTED GLOBAL)
  set_target_properties(d3d8_wgpu PROPERTIES IMPORTED_LOCATION "${RECOMP_D3D8_WGPU_LIB}")
  add_dependencies(d3d8_wgpu d3d8_wgpu_rust)
  target_include_directories(recomp_dx PRIVATE "${RECOMP_D3D8_GENERATED_DIR}")
  target_compile_definitions(recomp_dx PRIVATE RECOMP_D3D8_WGPU=1 RECOMP_D3D8_RESOURCES=1)
  target_link_libraries(recomp_dx PUBLIC d3d8_wgpu)
  foreach(fw ApplicationServices CoreGraphics CoreVideo Carbon AppKit Foundation QuartzCore Metal CoreFoundation ColorSync CoreServices IOKit)
    find_library(RECOMP_FW_${fw} ${fw})
    if(RECOMP_FW_${fw})
      target_link_libraries(recomp_dx PUBLIC ${RECOMP_FW_${fw}})
    endif()
  endforeach()
endif()

# The same shims with every host callback a strong no-op: a binary linking
# these can neither draw nor play sound. The parity fixture and the profile
# tests use it.
add_library(recomp_dx_null OBJECT ${POP_DX_SOURCES})
add_dependencies(recomp_dx_null d3d8_com_tables)
target_include_directories(recomp_dx_null PRIVATE "${RECOMP_D3D8_GENERATED_DIR}")
target_link_libraries(recomp_dx_null PUBLIC recomp_runtime recomp_frame_dump)
pop_link_video(recomp_dx_null)
target_compile_definitions(recomp_dx_null PUBLIC RECOMP_NULL_HOST)
target_compile_options(recomp_dx_null PRIVATE ${POP_WARN_HOST})
pop_optimize(recomp_dx_null 1)
# Exercise the same Rust CPU storage through guest COM without creating a GPU.
if(TARGET d3d8_wgpu)
  add_dependencies(recomp_dx_null d3d8_abi_header)
  target_include_directories(recomp_dx_null PRIVATE "${RECOMP_D3D8_GENERATED_DIR}")
  target_compile_definitions(recomp_dx_null PUBLIC RECOMP_D3D8_RESOURCES=1)
  target_link_libraries(recomp_dx_null PUBLIC d3d8_wgpu)
  foreach(fw ApplicationServices CoreGraphics CoreVideo Carbon AppKit Foundation QuartzCore Metal CoreFoundation ColorSync CoreServices IOKit)
    if(RECOMP_FW_${fw})
      target_link_libraries(recomp_dx_null PUBLIC ${RECOMP_FW_${fw}})
    endif()
  endforeach()
endif()


if(NOT IOS AND NOT ANDROID)
# Descriptor/lifetime tests construct guest device state explicitly; no Rust
# adapter or GPU is used. This also builds the optional renderer's off path.
add_executable(d3d8_surface_tests tests/d3d8_surface_tests.cpp
  ${POP_ROOT}/runtime/tests/stub_recomp_call.cpp)
target_link_libraries(d3d8_surface_tests PRIVATE recomp_platform recomp_dx_null recomp_runtime)
target_compile_options(d3d8_surface_tests PRIVATE ${POP_WARN_HOST})
pop_optimize(d3d8_surface_tests 1)
pop_test_binary(d3d8_surface_tests)
add_test(NAME d3d8_surface_tests COMMAND d3d8_surface_tests)
set_tests_properties(d3d8_surface_tests PROPERTIES LABELS nogame)
if(TARGET d3d8_wgpu)
  add_executable(d3d8_render_tests tests/d3d8_render_tests.cpp
    ${POP_ROOT}/runtime/tests/stub_recomp_call.cpp)
  add_dependencies(d3d8_render_tests d3d8_abi_header)
  target_include_directories(d3d8_render_tests PRIVATE "${RECOMP_D3D8_GENERATED_DIR}")
  target_link_libraries(d3d8_render_tests PRIVATE recomp_platform recomp_dx recomp_runtime)
  target_compile_options(d3d8_render_tests PRIVATE ${POP_WARN_HOST})
  pop_optimize(d3d8_render_tests 1)
  pop_test_binary(d3d8_render_tests)
  add_test(NAME d3d8_render_tests COMMAND d3d8_render_tests)
  set_tests_properties(d3d8_render_tests PROPERTIES LABELS gpu)
  add_library(d3d8_abi_layout_test OBJECT tests/d3d8_abi_layout_test.c)
  add_dependencies(d3d8_abi_layout_test d3d8_abi_header)
  target_include_directories(d3d8_abi_layout_test PRIVATE "${RECOMP_D3D8_GENERATED_DIR}")
  target_sources(d3d8_surface_tests PRIVATE $<TARGET_OBJECTS:d3d8_abi_layout_test>)
  add_test(NAME d3d8_abi_cpp_check
    COMMAND ${CMAKE_CXX_COMPILER} -std=c++17 -Wall -Wextra -Werror
            -I${RECOMP_D3D8_GENERATED_DIR} -fsyntax-only -x c++
            ${CMAKE_CURRENT_SOURCE_DIR}/tests/d3d8_abi_layout_test.c)
  set_tests_properties(d3d8_abi_cpp_check PROPERTIES LABELS nogame)
endif()


# host_api.h is a C header too. The C11 consumer is an object the DX tests
# link; the header is also parsed as C++17 on its own.
add_library(host_api_header_test OBJECT tests/host_api_header_test.c)
target_include_directories(host_api_header_test PRIVATE ${POP_ROOT})
target_compile_options(host_api_header_test PRIVATE -Wall -Wextra)
pop_optimize(host_api_header_test 1)
add_test(NAME host_api_header_check
  COMMAND ${CMAKE_CXX_COMPILER} -std=c++17 -O1 -Wall -Wextra -Wno-unused-parameter
          -I${POP_ROOT} -fsyntax-only -x c++ ${CMAKE_CURRENT_SOURCE_DIR}/host_api.h)
set_tests_properties(host_api_header_check PROPERTIES LABELS nogame)

add_executable(dx_tests tests/dx_tests.cpp
  ${POP_ROOT}/runtime/tests/stub_recomp_call.cpp
  $<TARGET_OBJECTS:host_api_header_test>)
target_link_libraries(dx_tests PRIVATE recomp_platform recomp_dx recomp_runtime)
target_compile_options(dx_tests PRIVATE ${POP_WARN_HOST})
pop_optimize(dx_tests 1)
pop_test_binary(dx_tests)
add_test(NAME dx_tests COMMAND dx_tests WORKING_DIRECTORY ${POP_ROOT})
set_tests_properties(dx_tests PROPERTIES LABELS nogame)

# The virtual pad through the DirectInput joystick and XInput. Its own binary
# because it defines the host_pad_* callbacks strongly, which dx_tests must not.
add_executable(pad_tests tests/pad_tests.cpp
  ${POP_ROOT}/runtime/tests/stub_recomp_call.cpp)
target_link_libraries(pad_tests PRIVATE recomp_platform recomp_dx recomp_runtime)
target_compile_options(pad_tests PRIVATE ${POP_WARN_HOST})
pop_optimize(pad_tests 1)
pop_test_binary(pad_tests)
add_test(NAME pad_tests COMMAND pad_tests WORKING_DIRECTORY ${POP_ROOT})
set_tests_properties(pad_tests PROPERTIES LABELS nogame)

# No strong host callback anywhere: the weak defaults must carry the link.
# page_overlay.cpp because every host links it beside boot.cpp; the dispatch
# stub because no generated code is present.
add_executable(null_host_link tests/null_host_link.cpp
  ${POP_ROOT}/host/page_overlay.cpp
  ${POP_ROOT}/runtime/tests/stub_recomp_call.cpp)
target_include_directories(null_host_link PRIVATE ${POP_ROOT})
target_link_libraries(null_host_link PRIVATE recomp_platform recomp_runtime recomp_dx host_common)
target_compile_options(null_host_link PRIVATE ${POP_WARN_HOST})
pop_optimize(null_host_link 1)
pop_test_binary(null_host_link)
add_test(NAME null_host_link COMMAND null_host_link WORKING_DIRECTORY ${POP_ROOT})
set_tests_properties(null_host_link PROPERTIES LABELS nogame)
endif()

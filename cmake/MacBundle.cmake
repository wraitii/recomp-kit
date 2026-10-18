# recomp_mac_bundle(<target>): make <target> a .app in build/, named by
# RECOMP_APP_NAME, with Info.plist copied verbatim and the resources,
# core mods, texture pack and ad-hoc signature applied after the link.
function(recomp_mac_bundle target)
  # Core plugins are compiled during bundle finishing. Relink/repackage when
  # their source or manifests change, even if the host itself is unchanged.
  file(GLOB_RECURSE core_inputs CONFIGURE_DEPENDS
    ${RECOMP_GAME_DIR}/mods/core/*.c ${RECOMP_GAME_DIR}/mods/core/*.h
    ${RECOMP_GAME_DIR}/mods/core/*.toml)
  if(core_inputs)
    set_property(TARGET ${target} APPEND PROPERTY LINK_DEPENDS ${core_inputs})
  endif()
  # Info.plist is XML: a name such as "Black & White" must be escaped.
  set(RECOMP_GAME_NAME_XML "${RECOMP_GAME_NAME}")
  string(REPLACE "&" "&amp;" RECOMP_GAME_NAME_XML "${RECOMP_GAME_NAME_XML}")
  string(REPLACE "<" "&lt;" RECOMP_GAME_NAME_XML "${RECOMP_GAME_NAME_XML}")
  string(REPLACE ">" "&gt;" RECOMP_GAME_NAME_XML "${RECOMP_GAME_NAME_XML}")
  set(RECOMP_BUNDLE_VERSION "1")
  set(RECOMP_BUNDLE_SHORT_VERSION "0.1")
  if(RECOMP_VERSION)
    string(REGEX REPLACE "^v" "" RECOMP_BUNDLE_VERSION "${RECOMP_VERSION}")
    set(RECOMP_BUNDLE_SHORT_VERSION "${RECOMP_BUNDLE_VERSION}")
  endif()
  configure_file(${RECOMP_ROOT}/host/Info.plist.in ${CMAKE_BINARY_DIR}/generated/Info.plist @ONLY)
  set_target_properties(${target} PROPERTIES
    MACOSX_BUNDLE ON
    OUTPUT_NAME ${RECOMP_APP_NAME}
    RUNTIME_OUTPUT_DIRECTORY ${RECOMP_BUILD_DIR}
    MACOSX_BUNDLE_INFO_PLIST ${CMAKE_BINARY_DIR}/generated/Info.plist)
  set(video_args)
  if(RECOMP_VIDEO)
    # Use the shipped libraries even in a developer build; no build-tree rpath
    # may make an incomplete bundle appear to work.
    set_target_properties(${target} PROPERTIES
      BUILD_WITH_INSTALL_RPATH ON
      INSTALL_RPATH "@executable_path/../Frameworks")
    set_property(TARGET ${target} APPEND PROPERTY LINK_DEPENDS
      ${RECOMP_ROOT}/third_party/ffmpeg/NOTICE.md)
    foreach(library IN LISTS RECOMP_FFMPEG_LIBRARIES)
      list(APPEND video_args --ffmpeg-library "${library}")
    endforeach()
  endif()
  set(aux_args)
  if(RECOMP_AUX_SHARED)
    # The auxiliary translations are dylibs beside the executable; the runtime
    # and main table stay in the executable and are exported for them.
    set_target_properties(${target} PROPERTIES
      BUILD_WITH_INSTALL_RPATH ON
      INSTALL_RPATH "@executable_path/../Frameworks")
    target_link_options(${target} PRIVATE "-Wl,-export_dynamic")
    foreach(aux_target ${RECOMP_GEN_AUX_TARGETS})
      list(APPEND aux_args --aux-dylib "$<TARGET_FILE:${aux_target}>")
    endforeach()
  endif()
  set_property(TARGET ${target} APPEND PROPERTY LINK_DEPENDS
    ${CMAKE_BINARY_DIR}/generated/Info.plist
    ${RECOMP_ROOT}/tools/recomp/finish_bundle.py
    ${RECOMP_ROOT}/tools/app_icons.py
    ${RECOMP_ROOT}/tools/extract_icon.py)
  file(GLOB icon_sidecars CONFIGURE_DEPENDS
    "${RECOMP_DEVELOPER_GAME_DIR}/*.[iI][cC][oO]")
  foreach(icon_source "${RECOMP_DEVELOPER_EXE}" ${icon_sidecars})
    if(EXISTS "${icon_source}")
      set_property(TARGET ${target} APPEND PROPERTY LINK_DEPENDS "${icon_source}")
    endif()
  endforeach()
  add_custom_command(TARGET ${target} POST_BUILD
    COMMAND ${Python3_EXECUTABLE} ${RECOMP_ROOT}/tools/recomp/finish_bundle.py
            --bundle ${RECOMP_BUILD_DIR}/${RECOMP_APP_NAME}.app
            --name ${RECOMP_APP_NAME} --cc ${CMAKE_C_COMPILER} --version ${RECOMP_VERSION}
            --build-root ${RECOMP_BUILD_ROOT} --game-dir ${RECOMP_GAME_DIR}
            ${video_args} ${aux_args}
    WORKING_DIRECTORY ${RECOMP_ROOT}
    COMMENT "Finishing ${RECOMP_APP_NAME}.app"
    VERBATIM)
endfunction()

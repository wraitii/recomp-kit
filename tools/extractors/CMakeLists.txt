cmake_minimum_required(VERSION 3.24)
project(RecompExtractors LANGUAGES C)
set(CMAKE_C_STANDARD 11)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(USE_OUR_OWN_MD5 ON CACHE BOOL "" FORCE)
set(BUILD_STATIC OFF CACHE BOOL "" FORCE)
set(CMAKE_DISABLE_FIND_PACKAGE_OpenSSL ON)
add_subdirectory(../../third_party/unshield unshield)
target_compile_definitions(libunshield PRIVATE UNSHIELD_EXPORTS PROTOTYPES=1)
target_compile_definitions(md5 PRIVATE PROTOTYPES=1)

option(RECOMP_BUILD_INNOEXTRACT "Build the portable Inno Setup extractor" OFF)
if(RECOMP_BUILD_INNOEXTRACT)
  include(ExternalProject)
  set(extractor_install ${CMAKE_BINARY_DIR}/install)
  set(extractor_sources ${CMAKE_BINARY_DIR}/sources)
  set(extractor_args
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5
    -DCMAKE_BUILD_TYPE=Release
    -DCMAKE_INSTALL_PREFIX=${extractor_install}
    -DCMAKE_INSTALL_LIBDIR=lib
    -DCMAKE_MAKE_PROGRAM=${CMAKE_MAKE_PROGRAM}
    -DCMAKE_C_COMPILER=${CMAKE_C_COMPILER}
    -DCMAKE_OSX_DEPLOYMENT_TARGET=${CMAKE_OSX_DEPLOYMENT_TARGET}
    -DCMAKE_OSX_SYSROOT=${CMAKE_OSX_SYSROOT}
    -DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew|/usr/local
    -DBUILD_SHARED_LIBS=OFF
    -DBUILD_TESTING=OFF)

  ExternalProject_Add(extractor_xz
    URL https://github.com/tukaani-project/xz/releases/download/v5.8.1/xz-5.8.1.tar.gz
    URL_HASH SHA256=507825b599356c10dca1cd720c9d0d0c9d5400b9de300af00e4d1ea150795543
    DOWNLOAD_DIR ${CMAKE_BINARY_DIR}/../source-archives
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    SOURCE_DIR ${extractor_sources}/xz
    LIST_SEPARATOR |
    CMAKE_ARGS ${extractor_args}
      -DXZ_TOOL_XZ=OFF -DXZ_TOOL_XZDEC=OFF -DXZ_TOOL_LZMADEC=OFF
      -DXZ_TOOL_LZMAINFO=OFF -DXZ_TOOL_SCRIPTS=OFF -DXZ_NLS=OFF)

  ExternalProject_Add(extractor_boost
    URL https://github.com/boostorg/boost/releases/download/boost-1.82.0/boost-1.82.0.tar.xz
    URL_HASH SHA256=fd60da30be908eff945735ac7d4d9addc7f7725b1ff6fcdcaede5262d511d21e
    DOWNLOAD_DIR ${CMAKE_BINARY_DIR}/../source-archives
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    SOURCE_DIR ${extractor_sources}/boost
    LIST_SEPARATOR |
    CMAKE_ARGS ${extractor_args}
      -DBOOST_INCLUDE_LIBRARIES=iostreams|filesystem|date_time|system|program_options|algorithm|container|foreach|format|lexical_cast|ptr_container|typeof|unordered
      -DBOOST_IOSTREAMS_ENABLE_ZSTD=OFF -DBOOST_IOSTREAMS_ENABLE_LZMA=OFF
      -DCMAKE_DISABLE_FIND_PACKAGE_ICU=ON)

  ExternalProject_Add(innoextract_external
    DEPENDS extractor_xz extractor_boost
    URL https://codeload.github.com/dscharrer/innoextract/tar.gz/refs/tags/1.9
    URL_HASH SHA256=576657932e92835343a829c3be1baefc34593d1f84309df93eb1d39a8bb51510
    DOWNLOAD_NAME innoextract-1.9.tar.gz
    DOWNLOAD_DIR ${CMAKE_BINARY_DIR}/../source-archives
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    SOURCE_DIR ${extractor_sources}/innoextract
    LIST_SEPARATOR |
    CMAKE_ARGS ${extractor_args}
      -DCMAKE_PREFIX_PATH=${extractor_install}
      -DBoost_NO_SYSTEM_PATHS=ON -DBoost_USE_STATIC_LIBS=ON
      -DLZMA_USE_STATIC_LIBS=ON
      -DLZMA_LIBRARY=${extractor_install}/lib/liblzma.a
      -DLZMA_INCLUDE_DIR=${extractor_install}/include
      -DUSE_LTO=OFF -DSET_WARNING_FLAGS=OFF)
endif()

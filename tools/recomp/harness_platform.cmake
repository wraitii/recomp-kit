# Standalone C harnesses map the guest arena at its fixed address (RECOMP_ARENA
# in runtime/x86.h) through the platform layer. Include after project() and
# link ${KIT_OS_SOURCES} into each executable that includes x86.h.
enable_language(CXX)
get_filename_component(KIT_ROOT "${KIT_RUNTIME}/.." ABSOLUTE)
if(WIN32)
  set(KIT_OS_SOURCES "${KIT_ROOT}/platform/os_win32.cpp")
  set(KIT_OS_LIBRARIES ws2_32)
else()
  set(KIT_OS_SOURCES "${KIT_ROOT}/platform/os_posix.cpp")
  set(KIT_OS_LIBRARIES m)
  if(NOT APPLE)
    list(APPEND KIT_OS_LIBRARIES dl pthread)
  endif()
endif()

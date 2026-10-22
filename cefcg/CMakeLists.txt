# cefcg.exe: Playout's built-in web browser for web overlays (see cefcg.cc).
# CI downloads the CEF "minimal" distribution and passes its folder as -DCEF_ROOT=...
cmake_minimum_required(VERSION 3.21)
project(cefcg)

set_property(GLOBAL PROPERTY OS_FOLDERS ON)
list(APPEND CMAKE_MODULE_PATH "${CEF_ROOT}/cmake")
find_package(CEF REQUIRED)
add_subdirectory(${CEF_LIBCEF_DLL_WRAPPER_PATH} libcef_dll_wrapper)

set(CEF_TARGET cefcg)
set(CEF_TARGET_OUT_DIR "${CMAKE_CURRENT_BINARY_DIR}/$<CONFIGURATION>")   # next to cefcg.exe
ADD_LOGICAL_TARGET("libcef_lib" "${CEF_LIB_DEBUG}" "${CEF_LIB_RELEASE}")
add_executable(${CEF_TARGET} WIN32 cefcg.cc)
SET_EXECUTABLE_TARGET_PROPERTIES(${CEF_TARGET})
add_dependencies(${CEF_TARGET} libcef_dll_wrapper)
target_link_libraries(${CEF_TARGET} libcef_lib libcef_dll_wrapper ${CEF_STANDARD_LIBS})
COPY_FILES("${CEF_TARGET}" "${CEF_BINARY_FILES}" "${CEF_BINARY_DIR}" "${CEF_TARGET_OUT_DIR}")
COPY_FILES("${CEF_TARGET}" "${CEF_RESOURCE_FILES}" "${CEF_RESOURCE_DIR}" "${CEF_TARGET_OUT_DIR}")

# pgpu.exe: Playout's native GPU engine (see pgpu.cc): Direct3D 11 mixer, the web pages on the GPU, NVENC.
# CI passes the FFmpeg "shared" development build as -DFFMPEG_ROOT=... (include\, lib\, bin\).
if(FFMPEG_ROOT)
  add_executable(pgpu WIN32 pgpu.cc)
  SET_EXECUTABLE_TARGET_PROPERTIES(pgpu)
  target_compile_options(pgpu PRIVATE /WX- /W3 /wd4996)
  target_include_directories(pgpu PRIVATE "${FFMPEG_ROOT}/include")
  add_dependencies(pgpu libcef_dll_wrapper)
  target_link_libraries(pgpu libcef_lib libcef_dll_wrapper ${CEF_STANDARD_LIBS} d3d11 dxgi d3dcompiler
    "${FFMPEG_ROOT}/lib/avcodec.lib" "${FFMPEG_ROOT}/lib/avformat.lib" "${FFMPEG_ROOT}/lib/avutil.lib"
    "${FFMPEG_ROOT}/lib/swscale.lib")
endif()

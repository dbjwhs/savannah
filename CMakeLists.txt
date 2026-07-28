# MIT License
# Copyright (c) 2026 Dennis B Jones

cmake_minimum_required(VERSION 3.16)
project(savannah VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Warning flags - NO WARNINGS ALLOWED (house rule, matches song)
add_compile_options(-Wall -Wextra -Werror)

# ---- song substrate -------------------------------------------------------
# song checked out and BUILT as a sibling by default; override with
# -DSONG_ROOT=... Build song first:
#   cmake -B build && cmake --build build --target song songc
# (song's CMake is not add_subdirectory-friendly yet: its test/ dir assumes
# top-level context. On the song wishlist: gate subdirs on PROJECT_IS_TOP_LEVEL.)
set(SONG_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../song" CACHE PATH
    "Path to the song repository (must be built: libsong.a + songc)")
set(SONG_LIB ${SONG_ROOT}/build/libsong.a)
set(SONGC ${SONG_ROOT}/build/songc)
if(NOT EXISTS ${SONG_LIB} OR NOT EXISTS ${SONGC})
    message(FATAL_ERROR
        "song not built at ${SONG_ROOT}/build. Run:\n"
        "  git clone https://github.com/dbjwhs/song ${SONG_ROOT}\n"
        "  cmake -S ${SONG_ROOT} -B ${SONG_ROOT}/build && "
        "cmake --build ${SONG_ROOT}/build --target song songc")
endif()

add_library(song STATIC IMPORTED)
set_target_properties(song PROPERTIES
    IMPORTED_LOCATION ${SONG_LIB}
    INTERFACE_INCLUDE_DIRECTORIES ${SONG_ROOT}/include)
# Mirror song's own platform link deps.
if(APPLE)
    find_library(DNSSD_LIBRARY dns_sd)
    if(DNSSD_LIBRARY)
        set_property(TARGET song APPEND PROPERTY
            INTERFACE_LINK_LIBRARIES ${DNSSD_LIBRARY})
    endif()
elseif(UNIX)
    find_package(OpenSSL)
    if(OpenSSL_FOUND)
        set_property(TARGET song APPEND PROPERTY
            INTERFACE_LINK_LIBRARIES OpenSSL::Crypto)
    endif()
endif()

# ---- IDL code generation ---------------------------------------------------
set(GEN_DIR ${CMAKE_CURRENT_BINARY_DIR}/generated)
file(MAKE_DIRECTORY ${GEN_DIR})
add_custom_command(
    OUTPUT ${GEN_DIR}/savannah.hpp
    COMMAND ${SONGC} ${CMAKE_CURRENT_SOURCE_DIR}/idl/agent.song -o ${GEN_DIR}/
    DEPENDS ${SONGC} ${CMAKE_CURRENT_SOURCE_DIR}/idl/agent.song
    COMMENT "songc: idl/agent.song -> generated/savannah.hpp")
add_custom_target(savannah_idl DEPENDS ${GEN_DIR}/savannah.hpp)

# ---- core library ----------------------------------------------------------
add_library(savannah_core STATIC
    src/config.cpp
    src/json.cpp
    src/stream_json.cpp
    src/agent_process.cpp
)
target_include_directories(savannah_core PUBLIC src ${GEN_DIR})
target_link_libraries(savannah_core PUBLIC song)
add_dependencies(savannah_core savannah_idl)

# ---- binaries ---------------------------------------------------------------
add_executable(savannahd src/savannahd.cpp)
target_link_libraries(savannahd PRIVATE savannah_core)

add_executable(savannah cli/savannah_main.cpp)
target_link_libraries(savannah PRIVATE savannah_core)

add_executable(fake-claude tools/fake_claude.cpp)

# ---- tests ------------------------------------------------------------------
enable_testing()

foreach(t test_config test_json test_stream_json)
    add_executable(${t} test/${t}.cpp)
    target_link_libraries(${t} PRIVATE savannah_core)
    add_test(NAME ${t} COMMAND ${t})
endforeach()

add_executable(test_integration_echo test/test_integration_echo.cpp)
target_link_libraries(test_integration_echo PRIVATE savannah_core)
add_test(NAME test_integration_echo COMMAND test_integration_echo
         WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
set_tests_properties(test_integration_echo PROPERTIES TIMEOUT 60)

# Integration test fixtures: per-scenario savannahd configs pointing at
# fake-claude. Generated at configure time into the build dir.
set(FAKE ${CMAKE_CURRENT_BINARY_DIR}/fake-claude)
function(scenario_config NAME SCENARIO EXTRA)
    file(WRITE ${CMAKE_CURRENT_BINARY_DIR}/cfg_${NAME}.toml
"[node]
name = \"${NAME}-node\"
workspace = \"/tmp\"
tags = [\"test\", \"${NAME}\"]

[agent]
cmd = \"${FAKE}\"
args = [\"--scenario\", \"${SCENARIO}\"${EXTRA}]
timeout_ms = 30000
")
endfunction()
scenario_config(echo echo "")
scenario_config(tools tool_storm ", \"--chunks\", \"3\"")
scenario_config(badjson bad_json "")
scenario_config(die die_before_result "")
scenario_config(hang hang "")

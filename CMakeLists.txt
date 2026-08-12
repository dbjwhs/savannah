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
    # Mirror song's optional Avahi dependency: when libsong.a was built with
    # SONG_HAS_AVAHI, its discovery code references avahi-client symbols.
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
        pkg_check_modules(AVAHI QUIET avahi-client)
        if(AVAHI_FOUND)
            set_property(TARGET song APPEND PROPERTY
                INTERFACE_LINK_LIBRARIES ${AVAHI_LINK_LIBRARIES})
        endif()
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
    src/task_supervisor.cpp
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

add_executable(test_live_schema test/test_live_schema.cpp)
target_link_libraries(test_live_schema PRIVATE savannah_core)
add_test(NAME test_live_schema COMMAND test_live_schema)
set_tests_properties(test_live_schema PROPERTIES
    SKIP_RETURN_CODE 77 TIMEOUT 180)

add_executable(test_tcp_flight test/test_tcp_flight.cpp)
target_link_libraries(test_tcp_flight PRIVATE savannah_core)
add_test(NAME test_tcp_flight COMMAND test_tcp_flight
         WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
set_tests_properties(test_tcp_flight PROPERTIES TIMEOUT 60)

add_executable(test_secure test/test_secure.cpp)
target_link_libraries(test_secure PRIVATE savannah_core)
add_test(NAME test_secure COMMAND test_secure
         WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
set_tests_properties(test_secure PROPERTIES TIMEOUT 60)

add_executable(test_mdns test/test_mdns.cpp)
target_link_libraries(test_mdns PRIVATE savannah_core)
add_test(NAME test_mdns COMMAND test_mdns
         WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
set_tests_properties(test_mdns PROPERTIES SKIP_RETURN_CODE 77 TIMEOUT 60)

add_executable(test_task_flight test/test_task_flight.cpp)
target_link_libraries(test_task_flight PRIVATE savannah_core)
add_test(NAME test_task_flight COMMAND test_task_flight
         WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
set_tests_properties(test_task_flight PROPERTIES SKIP_RETURN_CODE 77 TIMEOUT 90)

# MCP shim test: real MCP frames against shim/peer.py backed by the fake
# CLI. Pure stdlib Python; skipped only if no interpreter is found.
find_package(Python3 COMPONENTS Interpreter QUIET)
if(Python3_Interpreter_FOUND)
    add_test(NAME test_shim COMMAND ${Python3_EXECUTABLE}
             ${CMAKE_CURRENT_SOURCE_DIR}/test/test_shim.py
             ${CMAKE_CURRENT_SOURCE_DIR}/shim/peer.py
             ${CMAKE_CURRENT_SOURCE_DIR}/tools/fake_savannah.py)
    set_tests_properties(test_shim PROPERTIES TIMEOUT 60)
else()
    message(STATUS "Python3 not found - shim test disabled")
endif()

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
scenario_config(giant giant_chunk ", \"--giant-bytes\", \"4194304\"")
scenario_config(utf8 utf8_split "")
scenario_config(drip chunks ", \"--chunks\", \"6\", \"--delay-ms\", \"150\"")
scenario_config(noise stderr_noise "")
scenario_config(slow echo ", \"--initial-delay-ms\", \"6000\"")

# Task supervisor fixture: session-aware fake agent, workspace = a scratch git
# repo the test creates at runtime (worktrees need a real repo).
file(WRITE ${CMAKE_CURRENT_BINARY_DIR}/cfg_task.toml
"[node]
name = \"task-node\"
workspace = \"${CMAKE_CURRENT_BINARY_DIR}/task_repo\"
tags = [\"test\", \"task\"]

[agent]
cmd = \"${FAKE}\"
args = [\"--scenario\", \"session\"]
timeout_ms = 30000
")

# HMAC fixtures: deterministic 32+ byte keys and a secured echo node config.
file(WRITE ${CMAKE_CURRENT_BINARY_DIR}/test.key
     "savannah-test-hmac-key-0123456789abcdef")
file(WRITE ${CMAKE_CURRENT_BINARY_DIR}/test_wrong.key
     "savannah-WRONG-hmac-key-0123456789abcdef")
file(WRITE ${CMAKE_CURRENT_BINARY_DIR}/cfg_secure.toml
"[node]
name = \"secure-node\"
workspace = \"/tmp\"
tags = [\"test\", \"secure\"]

[agent]
cmd = \"${FAKE}\"
args = [\"--scenario\", \"echo\"]
timeout_ms = 30000

[mesh]
hmac_key_file = \"test.key\"
")
file(WRITE ${CMAKE_CURRENT_BINARY_DIR}/cfg_mdns.toml
"[node]
name = \"savannah-mdns-selftest\"
workspace = \"/tmp\"
tags = [\"test\", \"mdns\"]

[agent]
cmd = \"${FAKE}\"
args = [\"--scenario\", \"echo\"]
timeout_ms = 30000

[mesh]
hmac_key_file = \"test.key\"
")
scenario_config(die die_before_result "")
scenario_config(hang hang "")

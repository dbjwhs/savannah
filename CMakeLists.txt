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
# (song's CMake has gated its subdirs on top-level context since song main
# 4e989ed, finding 3, so add_subdirectory would work now. savannah still
# links the prebuilt libsong.a: one song build serves every consumer.)
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
# `savannah hub` / `savannah work` exec this module, and a bare `savannah`
# execs the Go dash (env SAVANNAH_WORK_PY / SAVANNAH_DASH_BIN override).
target_compile_definitions(savannah PRIVATE
    SAVANNAH_WORK_PY="${CMAKE_CURRENT_SOURCE_DIR}/work/savannah_work.py"
    SAVANNAH_DASH_BIN="${CMAKE_CURRENT_SOURCE_DIR}/dash/savannah-dash")

# The dash is a separate Go module; `cmake --build build --target dash` builds
# it in place when Go is installed (never part of ALL, never a test dependency).
find_program(GO_EXECUTABLE go)
if(GO_EXECUTABLE)
    add_custom_target(dash
        COMMAND ${GO_EXECUTABLE} build -o savannah-dash .
        WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/dash
        COMMENT "go build dash/savannah-dash")
endif()

add_executable(fake-claude tools/fake_claude.cpp)

# ---- tests ------------------------------------------------------------------
enable_testing()

# One C++ test: executable test/<name>.cpp linked against savannah_core,
# registered with ctest under the same name.
#   TIMEOUT n  ctest timeout in seconds (ctest's default when absent)
#   SKIP77     exit code 77 reports Skipped (opt-in and tool-gated tests)
#   DAEMON     an integration test that spawns ./savannahd and ./fake-claude:
#              runs with cwd = build dir (binaries + generated fixtures) and
#              builds after both targets
function(savannah_test NAME)
    cmake_parse_arguments(T "SKIP77;DAEMON" "TIMEOUT" "" ${ARGN})
    add_executable(${NAME} test/${NAME}.cpp test/test_support.hpp)
    target_link_libraries(${NAME} PRIVATE savannah_core)
    if(T_DAEMON)
        add_dependencies(${NAME} savannahd fake-claude)
        add_test(NAME ${NAME} COMMAND ${NAME}
                 WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
    else()
        add_test(NAME ${NAME} COMMAND ${NAME})
    endif()
    if(T_TIMEOUT)
        set_tests_properties(${NAME} PROPERTIES TIMEOUT ${T_TIMEOUT})
    endif()
    if(T_SKIP77)
        set_tests_properties(${NAME} PROPERTIES SKIP_RETURN_CODE 77)
    endif()
endfunction()

savannah_test(test_config)
savannah_test(test_json)
savannah_test(test_stream_json)
savannah_test(test_integration_echo DAEMON TIMEOUT 60)
savannah_test(test_live_schema SKIP77 TIMEOUT 180)
savannah_test(test_tcp_flight DAEMON TIMEOUT 60)
savannah_test(test_secure DAEMON TIMEOUT 60)
savannah_test(test_mdns DAEMON SKIP77 TIMEOUT 60)
savannah_test(test_task_flight DAEMON SKIP77 TIMEOUT 90)

# One Python suite: test/<name>.py run by the found interpreter, the
# remaining arguments passed through (the paths of what it tests). Pure
# stdlib Python; all of them are disabled only if no interpreter is found.
function(py_test NAME TIMEOUT)
    add_test(NAME ${NAME} COMMAND ${Python3_EXECUTABLE}
             ${CMAKE_CURRENT_SOURCE_DIR}/test/${NAME}.py ${ARGN})
    set_tests_properties(${NAME} PROPERTIES TIMEOUT ${TIMEOUT})
endfunction()

set(SRC ${CMAKE_CURRENT_SOURCE_DIR})
find_package(Python3 COMPONENTS Interpreter QUIET)
if(Python3_Interpreter_FOUND)
    # MCP shim: real MCP frames against shim/peer.py backed by the fake CLI.
    py_test(test_shim 60 ${SRC}/shim/peer.py ${SRC}/tools/fake_savannah.py)
    py_test(test_hook 60 ${SRC}/hooks/session-status.py)
    py_test(test_install 60 ${SRC}/install.sh)
    py_test(test_work 120 ${SRC}/work/savannah_work.py ${SRC}/tools/fake_it2.py
            $<TARGET_FILE:savannah>)
    py_test(test_restore 120 ${SRC}/work/savannah_work.py
            ${SRC}/tools/fake_it2.py)
else()
    message(STATUS "Python3 not found - shim and hook tests disabled")
endif()

# Integration test fixtures: per-scenario savannahd configs pointing at
# fake-claude. Generated at configure time into the build dir.
set(FAKE ${CMAKE_CURRENT_BINARY_DIR}/fake-claude)
# scenario_config(NAME SCENARIO EXTRA [NODE name] [WORKSPACE dir] [KEYED])
# writes cfg_<NAME>.toml. EXTRA is appended to the agent args verbatim. The
# node is "<NAME>-node" in /tmp unless NODE / WORKSPACE say otherwise; KEYED
# adds the [mesh] section pointing at test.key.
function(scenario_config NAME SCENARIO EXTRA)
    cmake_parse_arguments(C "KEYED" "NODE;WORKSPACE" "" ${ARGN})
    if(NOT C_NODE)
        set(C_NODE "${NAME}-node")
    endif()
    if(NOT C_WORKSPACE)
        set(C_WORKSPACE "/tmp")
    endif()
    set(MESH "")
    if(C_KEYED)
        set(MESH "\n[mesh]\nhmac_key_file = \"test.key\"\n")
    endif()
    file(WRITE ${CMAKE_CURRENT_BINARY_DIR}/cfg_${NAME}.toml
"[node]
name = \"${C_NODE}\"
workspace = \"${C_WORKSPACE}\"
tags = [\"test\", \"${NAME}\"]

[agent]
cmd = \"${FAKE}\"
args = [\"--scenario\", \"${SCENARIO}\"${EXTRA}]
timeout_ms = 30000
${MESH}")
endfunction()
scenario_config(echo echo "")
scenario_config(tools tool_storm ", \"--chunks\", \"3\"")
scenario_config(badjson bad_json "")
scenario_config(giant giant_chunk ", \"--giant-bytes\", \"4194304\"")
scenario_config(utf8 utf8_split "")
scenario_config(drip chunks ", \"--chunks\", \"6\", \"--delay-ms\", \"150\"")
scenario_config(noise stderr_noise "")
scenario_config(slow echo ", \"--initial-delay-ms\", \"6000\"")
scenario_config(die die_before_result "")
scenario_config(hang hang "")

# Task supervisor fixture: session-aware fake agent, workspace = a scratch git
# repo the test creates at runtime (worktrees need a real repo).
scenario_config(task session ""
                WORKSPACE "${CMAKE_CURRENT_BINARY_DIR}/task_repo")

# HMAC fixtures: deterministic 32+ byte keys, a secured echo node config and
# the mDNS self-test node (--mdns hard-requires the key).
file(WRITE ${CMAKE_CURRENT_BINARY_DIR}/test.key
     "savannah-test-hmac-key-0123456789abcdef")
file(WRITE ${CMAKE_CURRENT_BINARY_DIR}/test_wrong.key
     "savannah-WRONG-hmac-key-0123456789abcdef")
scenario_config(secure echo "" KEYED)
scenario_config(mdns echo "" KEYED NODE savannah-mdns-selftest)

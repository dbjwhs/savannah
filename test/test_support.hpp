// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// test_support.hpp: the scaffolding every C++ test shares. Header-only.
//
//   CHECK / CHECK_THROWS_AS / finish   the assert-style harness
//   Daemon                             savannahd --tcp 0 as a supervised child
//   connect_tcp                        loopback or discovered endpoint, keyed
//                                      or plaintext, handshake done
//   ask                                one streamed ask, chunks collected
//
// The daemon and the fixtures are found relative to the cwd, so a test that
// uses Daemon needs cwd = build dir (ctest sets WORKING_DIRECTORY).

#pragma once

#include <song/song.hpp>
#include <song/security.hpp>

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <memory>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "savannah.hpp"
#include "agent_process.hpp"
#include "stream_json.hpp"

namespace savannah::test {

inline int g_failures = 0;

/// Epilogue for main(): prints "<name>: <ok>" when nothing failed (failures
/// were already printed by CHECK) and returns the process exit code.
inline int finish(const char* name, const char* ok = "all passed") {
    if (g_failures == 0) std::printf("%s: %s\n", name, ok);
    return g_failures == 0 ? 0 : 1;
}

/// TCP connect budget for a test client, in milliseconds.
inline constexpr int kConnectTimeoutMs = 5000;

/// The line savannahd --tcp prints on stdout once it is listening.
inline constexpr std::string_view kPortMarker = "SAVANNAHD_TCP_PORT=";

/// savannahd --tcp 0 as a child; the port comes back on its stdout.
/// Move-only: exactly one Daemon owns the child, and its destructor kills
/// and reaps it. A failed spawn is an inert Daemon (pid -1, port 0).
class Daemon {
public:
    pid_t pid = -1;
    song::u16 port = 0;

    Daemon() = default;
    Daemon(const Daemon&) = delete;
    Daemon& operator=(const Daemon&) = delete;
    Daemon(Daemon&& other) noexcept
        : pid(std::exchange(other.pid, -1)),
          port(std::exchange(other.port, 0)) {}
    Daemon& operator=(Daemon&& other) noexcept {
        if (this != &other) {
            stop();
            pid = std::exchange(other.pid, -1);
            port = std::exchange(other.port, 0);
        }
        return *this;
    }
    ~Daemon() { stop(); }

    /// Kill and reap the child, if any. Idempotent.
    void stop() noexcept {
        if (pid > 0) {
            kill(pid, SIGKILL);
            waitpid(pid, nullptr, 0);
        }
        pid = -1;
        port = 0;
    }

    /// Runs ./savannahd --tcp 0 [extra_args...] with SAVANNAHD_CONFIG set to
    /// config_path and waits for its port marker line.
    static Daemon spawn(const std::string& config_path,
                        const std::vector<std::string>& extra_args = {}) {
        Daemon d;
        // argv is built before the fork: the child only execs.
        std::vector<std::string> args = {"savannahd", "--tcp", "0"};
        args.insert(args.end(), extra_args.begin(), extra_args.end());
        std::vector<char*> argv = to_cargv(args);

        int fds[2];
        if (pipe(fds) != 0) return d;
        pid_t child = fork();
        if (child < 0) {
            close(fds[0]);
            close(fds[1]);
            return d;
        }
        if (child == 0) {
            dup2(fds[1], STDOUT_FILENO);
            close(fds[0]);
            close(fds[1]);
            setenv("SAVANNAHD_CONFIG", config_path.c_str(), 1);
            execv("./savannahd", argv.data());
            _exit(127);
        }
        close(fds[1]);
        std::string line;
        char c;
        while (read(fds[0], &c, 1) == 1 && c != '\n') line.push_back(c);
        close(fds[0]);
        if (line.rfind(kPortMarker, 0) == 0) {
            d.pid = child;
            d.port = static_cast<song::u16>(std::strtoul(
                line.c_str() + kPortMarker.size(), nullptr, 10));
        } else {
            kill(child, SIGKILL);
            waitpid(child, nullptr, 0);
        }
        return d;
    }
};

/// TCP connect plus handshake. A non-empty key wraps the transport in song's
/// HMAC SecureTransport; an empty key is a plaintext client. Throws what song
/// throws (refused connect, failed handshake).
inline song::ServiceConnection connect_tcp(const std::string& host,
                                           song::u16 port,
                                           const std::string& key = "") {
    auto tcp = std::make_unique<song::TcpTransport>();
    tcp->connect(host, port, kConnectTimeoutMs);
    std::unique_ptr<song::Transport> transport = std::move(tcp);
    if (!key.empty()) {
        transport = std::make_unique<song::SecureTransport>(
            std::move(transport), song::SecurityConfig(key));
    }
    song::ServiceConnection conn(std::move(transport));
    conn.init_handshake();
    return conn;
}

/// True when a wire chunk (kind travels as i32) is of the given kind.
inline bool is_kind(const song::savannah::AgentChunk& c, ChunkKind kind) {
    return c.kind == static_cast<song::i32>(kind);
}

/// Everything one streamed ask produced.
struct Streamed {
    std::string text;                // TEXT payloads, concatenated
    std::vector<std::string> tools;  // TOOL_EVENT payloads, in order
    std::string result;              // the (last) RESULT trailer
    int chunk_count = 0;
    std::size_t max_payload = 0;

    /// True when the RESULT trailer reports a clean finish.
    bool ok() const { return ChunkMapper::result_ok(result); }

    void add(const song::savannah::AgentChunk& c) {
        ++chunk_count;
        if (c.payload.size() > max_payload) max_payload = c.payload.size();
        if (is_kind(c, ChunkKind::Text)) text += c.payload;
        if (is_kind(c, ChunkKind::ToolEvent)) tools.push_back(c.payload);
        if (is_kind(c, ChunkKind::Result)) result = c.payload;
    }
};

/// The AskOptions every test sends: fresh context, no hint.
inline song::savannah::AskOptions ask_options(song::u32 timeout_ms = 0) {
    song::savannah::AskOptions opts;
    opts.timeout_ms = timeout_ms;
    opts.fresh_context = true;
    opts.system_hint = "";
    return opts;
}

/// One ask through the generated proxy, on the incremental call_streaming
/// overload (chunks decode as they arrive). The chunk timeout is sized to
/// the agent, not the wire (song findings 14/15): 30 s covers the slow-start
/// scenario.
inline Streamed ask(song::ServiceConnection& conn, const std::string& prompt,
                    song::u32 timeout_ms = 0, int chunk_timeout_ms = 30000) {
    Streamed out;
    song::savannah::AgentNodeProxy(conn).ask(
        prompt, ask_options(timeout_ms),
        [&out](song::savannah::AgentChunk& c) { out.add(c); },
        chunk_timeout_ms);
    return out;
}

}  // namespace savannah::test

#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++::savannah::test::g_failures;                             \
        }                                                               \
    } while (0)

#define CHECK_THROWS_AS(expr, ExceptionType)                            \
    do {                                                                \
        bool threw = false;                                             \
        try { (void)(expr); } catch (const ExceptionType&) { threw = true; } \
        CHECK(threw);                                                   \
    } while (0)

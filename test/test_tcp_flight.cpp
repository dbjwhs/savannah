// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// Integration: single-flight and cancel semantics over concurrent clients.
//
// Pipe mode dispatches sequentially, so NodeBusy and cancel-while-busy can
// only fire when two clients hit the node at once: this test spawns
// savannahd --tcp 0 (run_tcp_multi, thread per client) against the hang
// scenario and drives it from two loopback connections.
//
// Requires cwd = build dir (ctest sets WORKING_DIRECTORY) with ./savannahd,
// ./fake-claude, and cfg_hang.toml.

#include <song/song.hpp>

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <thread>

#include "savannah.hpp"
#include "../src/wire_ids.hpp"

namespace sw = song::savannah;
using song::Buffer;
using song::ServiceConnection;
using song::u16;
using song::u32;

static int g_failures = 0;

#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                               \
        }                                                               \
    } while (0)

namespace {

// savannahd --tcp 0 as a child; the port comes back on its stdout.
struct Daemon {
    pid_t pid = -1;
    u16 port = 0;

    static Daemon spawn(const char* config) {
        Daemon d;
        int fds[2];
        if (pipe(fds) != 0) return d;
        pid_t pid = fork();
        if (pid < 0) {
            close(fds[0]);
            close(fds[1]);
            return d;
        }
        if (pid == 0) {
            dup2(fds[1], STDOUT_FILENO);
            close(fds[0]);
            close(fds[1]);
            setenv("SAVANNAHD_CONFIG", config, 1);
            execl("./savannahd", "savannahd", "--tcp", "0",
                  static_cast<char*>(nullptr));
            _exit(127);
        }
        close(fds[1]);
        // Read the SAVANNAHD_TCP_PORT=N marker line.
        std::string line;
        char c;
        while (read(fds[0], &c, 1) == 1 && c != '\n') line.push_back(c);
        close(fds[0]);
        const char* marker = "SAVANNAHD_TCP_PORT=";
        if (line.rfind(marker, 0) == 0) {
            d.pid = pid;
            d.port = static_cast<u16>(
                std::strtoul(line.c_str() + std::strlen(marker), nullptr, 10));
        } else {
            kill(pid, SIGKILL);
            waitpid(pid, nullptr, 0);
        }
        return d;
    }

    ~Daemon() {
        if (pid > 0) {
            kill(pid, SIGKILL);
            waitpid(pid, nullptr, 0);
        }
    }
};

ServiceConnection connect(u16 port) {
    auto tcp = std::make_unique<song::TcpTransport>();
    tcp->connect("127.0.0.1", port, 5000);
    ServiceConnection conn(std::move(tcp));
    conn.init_handshake();
    return conn;
}

struct Streamed {
    std::string text;
    std::string result;
    int chunk_count = 0;
};

Streamed ask(ServiceConnection& conn, const std::string& prompt,
             u32 timeout_ms = 0) {
    sw::AskOptions opts;
    opts.timeout_ms = timeout_ms;
    opts.fresh_context = true;
    opts.system_hint = "";

    Buffer args;
    song::encode_string(args, prompt);
    sw::encode_AskOptions(args, opts);

    auto reader = conn.call_streaming(
        savannah_wire::kService_AgentNode_Stream,
        sw::kMethod_AgentNode_ask, args);
    Streamed out;
    while (reader.next()) {
        auto c = sw::decode_AgentChunk(reader.chunk());
        ++out.chunk_count;
        if (c.kind == 0) out.text += c.payload;
        if (c.kind == 2) out.result = c.payload;
    }
    return out;
}

std::string status(ServiceConnection& conn) {
    Buffer req;
    Buffer resp = conn.call(sw::kService_AgentNode,
                            sw::kMethod_AgentNode_status, req);
    return song::decode_string(resp);
}

bool cancel(ServiceConnection& conn) {
    Buffer req;
    Buffer resp = conn.call(sw::kService_AgentNode,
                            sw::kMethod_AgentNode_cancel, req);
    return song::decode_bool(resp);
}

// Poll until the node reports the wanted status (the in-flight ask lands on
// another thread; give it a moment).
bool wait_status(ServiceConnection& conn, const std::string& want,
                 int budget_ms = 5000) {
    for (int waited = 0; waited < budget_ms; waited += 10) {
        if (status(conn) == want) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return false;
}

}  // namespace

int main() {
    Daemon d = Daemon::spawn("cfg_hang.toml");
    CHECK(d.pid > 0);
    CHECK(d.port != 0);
    if (g_failures) return 1;

    // ---- single-flight: a second ask while one hangs gets the busy
    //      trailer immediately, and the first flight is undisturbed ----
    {
        auto conn_a = connect(d.port);
        auto conn_b = connect(d.port);

        Streamed first;
        std::thread flight([&] { first = ask(conn_a, "never ends"); });

        CHECK(wait_status(conn_b, "busy"));

        auto second = ask(conn_b, "rejected");
        CHECK(second.chunk_count == 1);
        CHECK(second.result.find("\"is_error\":true") != std::string::npos);
        CHECK(second.result.find("busy") != std::string::npos);

        // Still busy: the rejected ask must not have clobbered the flight.
        CHECK(status(conn_b) == "busy");

        // Cleanup: cancel the hung flight so the thread joins fast.
        CHECK(cancel(conn_b) == true);
        flight.join();
        CHECK(first.result.find("\"is_error\":true") != std::string::npos);
        CHECK(wait_status(conn_b, "idle"));
    }

    if (g_failures == 0) std::printf("test_tcp_flight: all passed\n");
    return g_failures == 0 ? 0 : 1;
}

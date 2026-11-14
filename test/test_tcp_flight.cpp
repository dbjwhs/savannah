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

#include <chrono>
#include <cstdio>
#include <string>
#include <thread>

#include "savannah.hpp"
#include "test_support.hpp"

namespace sw = song::savannah;
namespace st = savannah::test;
using song::Buffer;
using song::ServiceConnection;
using st::Streamed;
using st::ask;

namespace {

ServiceConnection connect(song::u16 port) {
    return st::connect_tcp("127.0.0.1", port);
}

// The same ask on the eager StreamReader overload of call_streaming (the
// collector built on the incremental one, song's default 5 s chunk timeout).
// Every other ask in the suite goes through st::ask and the generated proxy;
// this is the one caller that keeps the eager overload covered.
Streamed ask_eager(ServiceConnection& conn, const std::string& prompt) {
    Buffer args;
    song::encode_string(args, prompt);
    sw::encode_AskOptions(args, st::ask_options());

    auto reader = conn.call_streaming(
        sw::kService_AgentNode_Stream,
        sw::kMethod_AgentNode_ask, args);
    Streamed out;
    while (reader.next()) out.add(sw::decode_AgentChunk(reader.chunk()));
    return out;
}

std::string status(ServiceConnection& conn) {
    return sw::AgentNodeProxy(conn).status();
}

bool cancel(ServiceConnection& conn) {
    return sw::AgentNodeProxy(conn).cancel();
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
    auto d = st::Daemon::spawn("cfg_hang.toml");
    CHECK(d.pid > 0);
    CHECK(d.port != 0);
    if (st::g_failures) return 1;

    // ---- single-flight: a second ask while one hangs gets the busy
    //      trailer immediately, and the first flight is undisturbed ----
    {
        auto conn_a = connect(d.port);
        auto conn_b = connect(d.port);

        Streamed first;
        std::thread flight([&] { first = ask(conn_a, "never ends"); });

        CHECK(wait_status(conn_b, "busy"));

        auto second = ask_eager(conn_b, "rejected");
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

    // ---- cancel: kills the in-flight ask (cancelled trailer), reports
    //      whether anything was cancelled, and leaves the node usable ----
    {
        auto conn_a = connect(d.port);
        auto conn_b = connect(d.port);

        Streamed flight_result;
        std::thread flight([&] { flight_result = ask(conn_a, "never ends"); });
        CHECK(wait_status(conn_b, "busy"));

        CHECK(cancel(conn_b) == true);
        flight.join();
        CHECK(flight_result.result.find("\"is_error\":true") !=
              std::string::npos);
        CHECK(flight_result.result.find("cancelled") != std::string::npos);
        CHECK(wait_status(conn_b, "idle"));

        // Nothing in flight anymore: cancel is a no-op reporting false.
        CHECK(cancel(conn_b) == false);

        // The gate cleared: a fresh ask runs (and times out on the hang
        // scenario with its own trailer, not a busy rejection).
        auto again = ask(conn_b, "runs again", /*timeout_ms=*/500);
        CHECK(again.result.find("\"is_error\":true") != std::string::npos);
        CHECK(again.result.find("timeout") != std::string::npos);
        CHECK(again.result.find("busy") == std::string::npos);
        CHECK(wait_status(conn_a, "idle"));
    }

    return st::finish("test_tcp_flight");
}

// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// mDNS self-discovery: savannahd --tcp 0 --mdns advertises on this machine,
// and the discovery client finds it, resolves it, and completes a keyed
// info() round-trip through the discovered host and port.
//
// Gated on SAVANNAH_MDNS=1 (exit 77 = ctest skip otherwise): it advertises
// on the real network, binds all interfaces, and mDNS is not a CI citizen.
// The two-machine version of this test is Phase 3 acceptance, run by hand.
//
// Requires cwd = build dir with ./savannahd, ./fake-claude, cfg_mdns.toml,
// and test.key.

#include <song/song.hpp>
#include <song/security.hpp>

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>

#include "savannah.hpp"
#include "../src/config.hpp"
#include "../src/wire_ids.hpp"

namespace sw = song::savannah;
using song::Buffer;
using song::ServiceConnection;
using song::u16;

static int g_failures = 0;

#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                               \
        }                                                               \
    } while (0)

namespace {

constexpr const char* kNodeName = "savannah-mdns-selftest";

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
            execl("./savannahd", "savannahd", "--tcp", "0", "--mdns",
                  static_cast<char*>(nullptr));
            _exit(127);
        }
        close(fds[1]);
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

}  // namespace

int main() {
    const char* opt_in = std::getenv("SAVANNAH_MDNS");
    if (!opt_in || std::string(opt_in) != "1") {
        std::printf("test_mdns: skipped (set SAVANNAH_MDNS=1 to advertise "
                    "on the local network)\n");
        return 77;
    }

    auto discovery = song::create_discovery();
    if (!discovery || !discovery->is_available()) {
        std::printf("test_mdns: skipped (mDNS not available here)\n");
        return 77;
    }

    Daemon d = Daemon::spawn("cfg_mdns.toml");
    CHECK(d.pid > 0);
    CHECK(d.port != 0);
    if (g_failures) return 1;

    // ---- browse: the node shows up under _agent._song._tcp ----
    auto found = discovery->discover_one(
        kNodeName, savannah_wire::kMeshServiceType,
        std::chrono::milliseconds(5000));
    CHECK(found.has_value());
    if (!found) return 1;
    CHECK(found->port == d.port);

    // ---- resolve + keyed round-trip through the discovered endpoint ----
    {
        const std::string key = savannah::load_hmac_key("test.key");
        auto tcp = std::make_unique<song::TcpTransport>();
        tcp->connect(found->host, found->port, 5000);
        std::unique_ptr<song::Transport> transport =
            std::make_unique<song::SecureTransport>(
                std::move(tcp), song::SecurityConfig(key));
        ServiceConnection conn(std::move(transport));
        conn.init_handshake();

        Buffer req;
        Buffer resp = conn.call(sw::kService_AgentNode,
                                sw::kMethod_AgentNode_info, req);
        auto info = sw::decode_AgentInfo(resp);
        CHECK(info.name == kNodeName);
        CHECK(info.protocol_ver == 1);
    }

    if (g_failures == 0) std::printf("test_mdns: all passed\n");
    return g_failures == 0 ? 0 : 1;
}

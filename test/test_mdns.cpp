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

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>

#include "savannah.hpp"
#include "test_support.hpp"
#include "config.hpp"
#include "wire_ids.hpp"

namespace sw = song::savannah;
namespace st = savannah::test;

namespace {

constexpr const char* kNodeName = "savannah-mdns-selftest";

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

    auto d = st::Daemon::spawn("cfg_mdns.toml", {"--mdns"});
    CHECK(d.pid > 0);
    CHECK(d.port != 0);
    if (st::g_failures) return 1;

    // ---- browse: the node shows up under _agent-song._tcp ----
    auto found = discovery->discover_one(
        kNodeName, savannah_wire::kMeshServiceType,
        std::chrono::milliseconds(5000));
    CHECK(found.has_value());
    if (!found) return 1;
    CHECK(found->port == d.port);

    // ---- resolve + keyed round-trip through the discovered endpoint ----
    {
        const std::string key = savannah::load_hmac_key("test.key");
        auto conn = st::connect_tcp(found->host, found->port, key);
        auto info = sw::AgentNodeProxy(conn).info();
        CHECK(info.name == kNodeName);
        CHECK(info.protocol_ver == 1);
    }

    return st::finish("test_mdns");
}

// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// savannah — the human's remote control (and the test harness).
//
//   savannah ls [--timeout-ms N]
//   savannah ask <node> "<prompt>" [options]
//   savannah info <node> [options]
//   savannah status <node> [options]
//
// <node> "local" spawns ./savannahd over pipes (Phase 1 path, no network).
// Any other <node> is a mesh name: resolved via mDNS (_agent-song._tcp), or
// taken from --addr host:port to skip discovery. --key FILE enables HMAC and
// is required to talk to any keyed node (which is every --mdns node).
//
// Output contract: TEXT chunks -> stdout verbatim; TOOL_EVENT -> stderr as
// "[tool] ..."; the RESULT trailer -> stderr as "[result] ...". Exit code 0
// iff the trailer has is_error:false. Scripts can pipe stdout cleanly.

#include <song/song.hpp>
#include <song/security.hpp>

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <set>
#include <string>

#include "savannah.hpp"
#include "../src/config.hpp"    // load_hmac_key
#include "../src/wire_ids.hpp"  // stream service id + mesh mDNS type

namespace sw = song::savannah;

using namespace song;

namespace {

int usage() {
    std::cerr <<
        "usage: savannah ls [--timeout-ms N]\n"
        "       savannah <ask|info|status> <node> [\"prompt\"]\n"
        "               [--config PATH] [--timeout-ms N]\n"
        "               [--addr HOST:PORT] [--key FILE]\n"
        "\"local\" spawns ./savannahd over pipes; any other node is resolved\n"
        "via mDNS (or --addr) and reached over TCP, HMAC'd when --key is set.\n";
    return 2;
}

struct Cli {
    std::string cmd;
    std::string node;
    std::string prompt;
    std::string config;
    std::string addr;
    std::string key_file;
    u32 timeout_ms = 0;
};

bool parse_cli(int argc, char** argv, Cli& c) {
    if (argc < 2) return false;
    c.cmd = argv[1];
    int i = 2;
    if (c.cmd != "ls") {
        if (argc < 3) return false;
        c.node = argv[2];
        i = 3;
        if (c.cmd == "ask") {
            if (argc < 4) return false;
            c.prompt = argv[3];
            i = 4;
        }
    }
    for (; i < argc; ++i) {
        if (std::strcmp(argv[i], "--config") == 0 && i + 1 < argc) {
            c.config = argv[++i];
        } else if (std::strcmp(argv[i], "--timeout-ms") == 0 && i + 1 < argc) {
            c.timeout_ms = static_cast<u32>(std::strtoul(argv[++i], nullptr, 10));
        } else if (std::strcmp(argv[i], "--addr") == 0 && i + 1 < argc) {
            c.addr = argv[++i];
        } else if (std::strcmp(argv[i], "--key") == 0 && i + 1 < argc) {
            c.key_file = argv[++i];
        } else {
            return false;
        }
    }
    return c.cmd == "ls" || c.cmd == "ask" || c.cmd == "info" ||
           c.cmd == "status";
}

int run_ls(const Cli& cli) {
    auto discovery = create_discovery();
    if (!discovery || !discovery->is_available()) {
        std::cerr << "savannah: mDNS not available on this machine\n";
        return 1;
    }
    auto timeout =
        std::chrono::milliseconds(cli.timeout_ms ? cli.timeout_ms : 2000);
    auto found =
        discovery->discover(savannah_wire::kMeshServiceType, timeout);
    if (found.empty()) {
        std::cerr << "savannah: no nodes on the mesh\n";
        return 0;
    }
    // mDNS reports a service once per interface; collapse duplicates.
    std::set<std::string> seen;
    for (const auto& s : found) {
        std::string row =
            s.name + " " + s.host + ":" + std::to_string(s.port);
        if (seen.insert(row).second) std::cout << row << "\n";
    }
    return 0;
}

// Resolve + connect to a remote node: --addr wins, else mDNS by name.
ServiceConnection connect_remote(const Cli& cli) {
    std::string host;
    u16 port = 0;
    if (!cli.addr.empty()) {
        auto colon = cli.addr.rfind(':');
        if (colon == std::string::npos) {
            throw std::runtime_error("--addr wants HOST:PORT");
        }
        host = cli.addr.substr(0, colon);
        port = static_cast<u16>(
            std::strtoul(cli.addr.c_str() + colon + 1, nullptr, 10));
    } else {
        auto discovery = create_discovery();
        if (!discovery || !discovery->is_available()) {
            throw std::runtime_error(
                "mDNS not available; use --addr HOST:PORT");
        }
        auto s = discovery->discover_one(
            cli.node, savannah_wire::kMeshServiceType,
            std::chrono::milliseconds(3000));
        if (!s) {
            throw std::runtime_error("node \"" + cli.node +
                                     "\" not found on the mesh");
        }
        host = s->host;
        port = s->port;
    }

    auto tcp = std::make_unique<TcpTransport>();
    tcp->connect(host, port, 5000);
    std::unique_ptr<Transport> transport = std::move(tcp);
    if (!cli.key_file.empty()) {
        transport = std::make_unique<SecureTransport>(
            std::move(transport),
            SecurityConfig(::savannah::load_hmac_key(cli.key_file)));
    }
    ServiceConnection conn(std::move(transport));
    conn.init_handshake();
    return conn;
}

int run_command(ServiceConnection& conn, const Cli& cli) {
    if (cli.cmd == "info") {
        Buffer req;
        Buffer resp = conn.call(sw::kService_AgentNode,
                                 sw::kMethod_AgentNode_info, req);
        auto info = sw::decode_AgentInfo(resp);
        std::cout << "name:      " << info.name << "\n"
                  << "model:     " << info.model << "\n"
                  << "workspace: " << info.workspace << "\n"
                  << "tags:      ";
        for (std::size_t i = 0; i < info.tags.size(); ++i) {
            std::cout << (i ? "," : "") << info.tags[i];
        }
        std::cout << "\nprotocol:  v" << info.protocol_ver << "\n";
        return 0;
    }

    if (cli.cmd == "status") {
        Buffer req;
        Buffer resp = conn.call(sw::kService_AgentNode,
                                 sw::kMethod_AgentNode_status, req);
        std::cout << decode_string(resp) << "\n";
        return 0;
    }

    // ask
    sw::AskOptions opts;
    opts.timeout_ms = cli.timeout_ms;
    opts.fresh_context = true;
    opts.system_hint = "";

    Buffer args;
    encode_string(args, cli.prompt);
    sw::encode_AskOptions(args, opts);

    auto reader = conn.call_streaming(
        savannah_wire::kService_AgentNode_Stream,
        sw::kMethod_AgentNode_ask, args);
    bool ok = false;
    while (reader.next()) {
        Buffer& chunk = reader.chunk();
        auto c = sw::decode_AgentChunk(chunk);
        switch (c.kind) {
            case 0:  // TEXT
                std::cout << c.payload;
                std::cout.flush();
                break;
            case 1:  // TOOL_EVENT
                std::cerr << "[tool] " << c.payload << "\n";
                break;
            case 2:  // RESULT
                std::cerr << "\n[result] " << c.payload << "\n";
                ok = c.payload.find("\"is_error\":false") !=
                     std::string::npos;
                break;
            default:
                std::cerr << "[?] unknown chunk kind " << c.kind << "\n";
                break;
        }
    }
    std::cout.flush();
    return ok ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
    Cli cli;
    if (!parse_cli(argc, argv, cli)) return usage();

    try {
        if (cli.cmd == "ls") return run_ls(cli);

        if (cli.node == "local") {
            if (!cli.config.empty()) {
                setenv("SAVANNAHD_CONFIG", cli.config.c_str(), 1);
            }
            ServiceManager mgr;
            mgr.register_service("agent", "./savannahd", 1);
            ServiceConnection conn = mgr.connect("agent");
            return run_command(conn, cli);
        }

        ServiceConnection conn = connect_remote(cli);
        return run_command(conn, cli);
    } catch (const std::exception& e) {
        std::cerr << "savannah: " << e.what() << "\n";
        return 1;
    }
}

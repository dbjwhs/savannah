// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// savannah — the human's remote control (and the Phase 1 test harness).
//
//   savannah ask <node> "<prompt>" [--config PATH] [--timeout-ms N]
//   savannah info <node> [--config PATH]
//   savannah status <node> [--config PATH]
//
// Phase 1: <node> must be "local". The CLI spawns ./savannahd via
// ServiceManager over pipes. Phase 3 adds mDNS: <node> becomes a mesh name.
//
// Output contract: TEXT chunks -> stdout verbatim; TOOL_EVENT -> stderr as
// "[tool] ..."; the RESULT trailer -> stderr as "[result] ...". Exit code 0
// iff the trailer has is_error:false. Scripts can pipe stdout cleanly.

#include <song/song.hpp>

#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>

#include "savannah.hpp"
#include "../src/wire_ids.hpp"  // generated

namespace sw = song::savannah;

using namespace song;

namespace {

int usage() {
    std::cerr <<
        "usage: savannah <ask|info|status> <node> [\"prompt\"]\n"
        "               [--config PATH] [--timeout-ms N]\n"
        "Phase 1: node must be \"local\" (spawns ./savannahd over pipes).\n";
    return 2;
}

struct Cli {
    std::string cmd;
    std::string node;
    std::string prompt;
    std::string config;
    u32 timeout_ms = 0;
};

bool parse_cli(int argc, char** argv, Cli& c) {
    if (argc < 3) return false;
    c.cmd = argv[1];
    c.node = argv[2];
    int i = 3;
    if (c.cmd == "ask") {
        if (argc < 4) return false;
        c.prompt = argv[3];
        i = 4;
    }
    for (; i < argc; ++i) {
        if (std::strcmp(argv[i], "--config") == 0 && i + 1 < argc) {
            c.config = argv[++i];
        } else if (std::strcmp(argv[i], "--timeout-ms") == 0 && i + 1 < argc) {
            c.timeout_ms = static_cast<u32>(std::strtoul(argv[++i], nullptr, 10));
        } else {
            return false;
        }
    }
    return c.cmd == "ask" || c.cmd == "info" || c.cmd == "status";
}

}  // namespace

int main(int argc, char** argv) {
    Cli cli;
    if (!parse_cli(argc, argv, cli)) return usage();

    if (cli.node != "local") {
        std::cerr << "savannah: only \"local\" is wired up in Phase 1 "
                     "(mDNS lands in Phase 3)\n";
        return 2;
    }
    if (!cli.config.empty()) {
        setenv("SAVANNAHD_CONFIG", cli.config.c_str(), 1);
    }

    try {
        ServiceManager mgr;
        mgr.register_service("agent", "./savannahd", 1);
        ServiceConnection conn = mgr.connect("agent");

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
    } catch (const std::exception& e) {
        std::cerr << "savannah: " << e.what() << "\n";
        return 1;
    }
}

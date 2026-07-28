// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// savannahd — one machine's agent, exposed as a song service.
//
// Phase 1: launched by ServiceManager over local pipes (stdin/stdout wire).
// Config comes from $SAVANNAHD_CONFIG or --config. The agent command comes
// from config, which is how tests substitute fake-claude for the real CLI:
// production code has zero test hooks.
//
// Single-flight: one ask at a time. A second ask while busy gets an
// immediate error RESULT chunk (see CLAUDE.md known limitations for why it
// is not a wire-level thrown error yet).

#include <song/song.hpp>
#include <song/logging.hpp>  // not in the umbrella header (song wishlist)

#include <atomic>
#include <cstdlib>
#include <mutex>
#include <string>
#include <vector>

#include "agent_process.hpp"
#include "config.hpp"
#include "savannah.hpp"  // generated from idl/agent.song
#include "stream_json.hpp"
#include "wire_ids.hpp"

// No `using namespace song`: our project namespace is also `savannah`, and
// the generated wire namespace is `song::savannah`. Aliases keep it sane.
namespace sw = song::savannah;
using song::Buffer;
using song::StreamWriter;
using song::u16;
using song::i32;

namespace {

savannah::NodeConfig g_config;

// Single-flight state.
std::mutex g_flight_mutex;          // held only for state transitions
std::atomic<bool> g_busy{false};
std::atomic<bool> g_cancel{false};

std::vector<std::string> build_agent_argv(const std::string& prompt,
                                          const sw::AskOptions& opts) {
    std::vector<std::string> argv;
    argv.push_back(g_config.agent_cmd);
    for (const auto& a : g_config.agent_args) argv.push_back(a);
    argv.push_back("-p");
    argv.push_back(prompt);
    argv.push_back("--output-format");
    argv.push_back("stream-json");
    argv.push_back("--verbose");
    argv.push_back("--max-turns");
    argv.push_back(std::to_string(g_config.max_turns));
    if (!g_config.allowed_tools.empty()) {
        argv.push_back("--allowedTools");
        std::string joined;
        for (const auto& t : g_config.allowed_tools) {
            if (!joined.empty()) joined += ",";
            joined += t;
        }
        argv.push_back(joined);
    }
    if (!opts.system_hint.empty()) {
        argv.push_back("--append-system-prompt");
        argv.push_back(opts.system_hint);
    }
    return argv;
}

void write_chunk(StreamWriter& writer, savannah::ChunkKind kind,
                 const std::string& payload) {
    sw::AgentChunk chunk;
    chunk.kind = static_cast<i32>(kind);
    chunk.payload = payload;
    Buffer buf;
    sw::encode_AgentChunk(buf, chunk);
    writer.write(buf);
}

void handle_ask(Buffer& request, StreamWriter& writer) {
    std::string prompt = song::decode_string(request);
    sw::AskOptions opts = sw::decode_AskOptions(request);

    // Single-flight gate.
    {
        std::lock_guard<std::mutex> lock(g_flight_mutex);
        if (g_busy.load()) {
            write_chunk(writer, savannah::ChunkKind::Result,
                        savannah::ChunkMapper::synthetic_error_result(
                            "busy", 0));
            return;
        }
        g_busy.store(true);
        g_cancel.store(false);
    }

    std::int64_t timeout =
        opts.timeout_ms > 0 ? static_cast<std::int64_t>(opts.timeout_ms)
                            : g_config.timeout_ms;

    savannah::AgentProcess proc;
    auto outcome = proc.run(
        build_agent_argv(prompt, opts), timeout, g_cancel,
        [&](const savannah::Chunk& c) {
            write_chunk(writer, c.kind, c.payload);
        });

    if (!outcome.saw_result) {
        std::string reason = !outcome.spawned ? "spawn failed"
                             : outcome.timed_out ? "timeout"
                             : outcome.killed    ? "cancelled"
                                                 : "died before result";
        write_chunk(writer, savannah::ChunkKind::Result,
                    savannah::ChunkMapper::synthetic_error_result(
                        reason, outcome.exit_code));
    }

    g_busy.store(false);
}

void stream_dispatcher(u16 method_id, Buffer& request, StreamWriter& writer) {
    if (method_id == sw::kMethod_AgentNode_ask) {
        handle_ask(request, writer);
        return;
    }
    throw std::runtime_error("unknown streaming method: " +
                             std::to_string(method_id));
}

void unary_dispatcher(u16 method_id, Buffer& request, Buffer& response) {
    switch (method_id) {
        case sw::kMethod_AgentNode_info: {
            sw::AgentInfo info;
            info.name = g_config.name;
            info.model = "unknown";  // v1: node does not probe the agent
            info.workspace = g_config.workspace;
            info.tags = g_config.tags;
            info.protocol_ver = 1;
            sw::encode_AgentInfo(response, info);
            break;
        }
        case sw::kMethod_AgentNode_cancel: {
            bool was_busy = g_busy.load();
            if (was_busy) g_cancel.store(true);
            song::encode_bool(response, was_busy);
            break;
        }
        case sw::kMethod_AgentNode_status: {
            song::encode_string(response, g_busy.load() ? "busy" : "idle");
            break;
        }
        default:
            (void)request;
            throw std::runtime_error("unknown method: " +
                                     std::to_string(method_id));
    }
}

}  // namespace

int main(int argc, char** argv) {
    std::string config_path;
    if (const char* env = std::getenv("SAVANNAHD_CONFIG")) config_path = env;
    for (int i = 1; i + 1 < argc; ++i) {
        if (std::string(argv[i]) == "--config") config_path = argv[i + 1];
    }
    if (config_path.empty()) {
        song::Log::error("savannahd: no config (set SAVANNAHD_CONFIG or --config)");
        return 2;
    }

    try {
        g_config = savannah::NodeConfig::from(
            savannah::Config::parse_file(config_path));
    } catch (const std::exception& e) {
        song::Log::error(std::string("savannahd: ") + e.what());
        return 2;
    }

    song::ServiceRuntime runtime;
    runtime.register_dispatcher(sw::kService_AgentNode,
                                unary_dispatcher);
    runtime.register_stream_dispatcher(
        savannah_wire::kService_AgentNode_Stream, stream_dispatcher);

    runtime.register_method(sw::kService_AgentNode,
                            sw::kMethod_AgentNode_info);
    runtime.register_method(sw::kService_AgentNode,
                            sw::kMethod_AgentNode_cancel);
    runtime.register_method(sw::kService_AgentNode,
                            sw::kMethod_AgentNode_status);
    runtime.register_method(savannah_wire::kService_AgentNode_Stream,
                            sw::kMethod_AgentNode_ask,
                            song::wire::MethodFlags::streaming);
    runtime.set_capability(song::wire::Capability::streaming);

    song::Log::debug("savannahd up: node=" + g_config.name +
               " agent=" + g_config.agent_cmd);
    runtime.run();
    return 0;
}

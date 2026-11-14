// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// agent_process.hpp — spawn and supervise one headless agent invocation.
//
// v1 semantics: fork/exec the configured command, pump stdout lines into a
// ChunkMapper until EOF, result, timeout, or kill. The child's stderr is
// inherited (savannahd's own stderr) so operator logs stay in one place.

#pragma once

#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

#include "stream_json.hpp"

namespace savannah {

struct NodeConfig;

struct AgentOutcome {
    bool spawned = false;      // exec happened
    bool saw_result = false;   // stream ended with a RESULT chunk
    bool timed_out = false;
    bool killed = false;       // cancel() arrived
    bool max_turns_reached = false;  // RESULT subtype was error_max_turns
    int exit_code = -1;
    int malformed_lines = 0;
};

/// Why a run ended without a RESULT chunk: the `reason` of the synthetic
/// error trailer. Precedence: spawn failed, timeout, cancelled, died.
const char* failure_reason(const AgentOutcome& outcome);

/// The argv every agent invocation starts with: the configured command and
/// its extra args, then -p PROMPT, stream-json output, the turn cap and the
/// allowed tools when any are configured. Callers append their own flags.
std::vector<std::string> base_agent_argv(const NodeConfig& config,
                                         const std::string& prompt);

/// A NULL-terminated char* view of `argv` for the exec family. The pointers
/// borrow from `argv`, which must outlive the returned vector; exec never
/// writes through them, which is what makes the const_cast sound.
inline std::vector<char*> to_cargv(const std::vector<std::string>& argv) {
    std::vector<char*> cargv;
    cargv.reserve(argv.size() + 1);
    for (const auto& a : argv) cargv.push_back(const_cast<char*>(a.c_str()));
    cargv.push_back(nullptr);
    return cargv;
}

class AgentProcess {
public:
    /// Run `argv` (argv[0] = binary), stream chunks into `sink`.
    /// Blocks until the child is done or timeout_ms expires.
    /// `cancel_flag` checked between reads; when set, child gets SIGKILL.
    /// `cwd`, if non-empty, is the child's working directory (chdir before
    /// exec) — task workers run inside their git worktree this way.
    AgentOutcome run(const std::vector<std::string>& argv,
                     std::int64_t timeout_ms,
                     const std::atomic<bool>& cancel_flag,
                     ChunkMapper::Sink sink,
                     const std::string& cwd = "");
};

}  // namespace savannah

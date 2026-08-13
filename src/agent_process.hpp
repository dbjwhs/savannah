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
#include <string>
#include <vector>

#include "stream_json.hpp"

namespace savannah {

struct AgentOutcome {
    bool spawned = false;      // exec happened
    bool saw_result = false;   // stream ended with a RESULT chunk
    bool timed_out = false;
    bool killed = false;       // cancel() arrived
    bool max_turns_reached = false;  // RESULT subtype was error_max_turns
    int exit_code = -1;
    int malformed_lines = 0;
};

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

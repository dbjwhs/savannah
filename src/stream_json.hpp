// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// stream_json.hpp — claude stream-json lines → AgentChunk events.
//
// Pinned contract (see CLAUDE.md "stream-json contract"):
//   assistant/text        → TEXT chunk per text block
//   assistant/tool_use    → TOOL_EVENT chunk ("name: summary", truncated)
//   result                → RESULT chunk (compact JSON trailer)
//   system, user, unknown → ignored (forward compatible)
//
// The mapper is a pure function of input lines: no I/O, trivially testable.

#pragma once

#include <functional>
#include <string>

namespace savannah {

enum class ChunkKind : int { Text = 0, ToolEvent = 1, Result = 2 };

struct Chunk {
    ChunkKind kind;
    std::string payload;
};

/// Feed stream-json lines, get Chunk callbacks.
/// Malformed JSON lines are counted and skipped (agents burp; we do not die).
class ChunkMapper {
public:
    using Sink = std::function<void(const Chunk&)>;

    explicit ChunkMapper(Sink sink) : sink_(std::move(sink)) {}

    /// Process one complete line (no trailing newline required).
    void feed_line(const std::string& line);

    /// True once a RESULT chunk has been emitted.
    bool saw_result() const { return saw_result_; }

    /// Lines that failed to parse as JSON (skipped).
    int malformed_lines() const { return malformed_; }

    /// True if the last RESULT had subtype "error_max_turns": the agent hit
    /// its per-invocation --max-turns cap with work still pending. That is
    /// continuable, not a real failure; the supervisor auto-continues on it.
    bool max_turns_reached() const {
        return result_subtype_ == "error_max_turns";
    }

    /// Build the RESULT payload savannahd synthesizes when the agent dies
    /// without emitting one.
    static std::string synthetic_error_result(const std::string& reason,
                                              int exit_code);

    /// Max length of a TOOL_EVENT summary payload.
    static constexpr std::size_t kToolSummaryMax = 120;

private:
    Sink sink_;
    bool saw_result_ = false;
    int malformed_ = 0;
    std::string result_subtype_;  // subtype of the last RESULT seen
};

}  // namespace savannah

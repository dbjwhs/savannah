// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// fake_claude — a stand-in for `claude -p --output-format stream-json`.
//
// Speaks the pinned stream-json subset (see CLAUDE.md) so savannahd cannot
// tell it from the real thing. Deterministic, token-free, and configurable
// into pathological moods for stress tests.
//
// Usage: fake-claude [claude-ish flags, ignored] -p <prompt>
//                    [--scenario NAME] [--chunks N] [--delay-ms D]
//                    [--giant-bytes N] [--initial-delay-ms D]
//
// --initial-delay-ms sleeps BEFORE any output, modeling a real agent's
// thinking time ahead of its first token (real claude routinely takes
// longer than song's old 5s per-chunk ceiling; see savannah finding 14).
//
// Scenarios:
//   echo              one text chunk: the prompt verbatim, then result (default)
//   chunks            prompt split across N text chunks, delay D between
//   tool_storm        N tool_use events, then a text chunk, then result
//   giant_chunk       one text chunk of N bytes ('x'), then result
//   die_before_result emit init + one text chunk, exit(3) with no result
//   hang              emit init, then sleep forever (tests timeout + cancel)
//   bad_json          emit garbage lines between valid ones, then result
//   utf8_split        one line flushed in two writes split inside a 4-byte
//                     codepoint, then a text block split across two chunks
//                     mid-codepoint, then result
//   stderr_noise      flood stderr with 128 KiB blobs (past pipe capacity)
//                     between valid stdout lines, then result
//
// Unknown flags are swallowed so callers can pass the same argv shape they
// would pass the real CLI.

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

namespace {

void emit(const std::string& line) {
    std::fputs(line.c_str(), stdout);
    std::fputc('\n', stdout);
    std::fflush(stdout);
}

std::string escape(const std::string& s) {
    std::string out;
    for (char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\t': out += "\\t"; break;
            default:   out.push_back(c);
        }
    }
    return out;
}

void emit_init() {
    emit(R"({"type":"system","subtype":"init","session_id":"fake-0000",)"
         R"("model":"fake-claude-1"})");
}

void emit_text(const std::string& text) {
    emit(R"({"type":"assistant","message":{"content":[{"type":"text",)"
         R"("text":")" + escape(text) + R"("}]}})");
}

void emit_tool(const std::string& name, const std::string& command) {
    emit(R"({"type":"assistant","message":{"content":[{"type":"tool_use",)"
         R"("name":")" + escape(name) + R"(","input":{"command":")" +
         escape(command) + R"("}}]}})");
}

void emit_result(bool is_error) {
    emit(R"({"type":"result","subtype":"success","total_cost_usd":0.00042,)"
         R"("duration_ms":1234,"num_turns":1,"is_error":)" +
         std::string(is_error ? "true" : "false") + "}");
}

}  // namespace

int main(int argc, char** argv) {
    std::string scenario = "echo";
    std::string prompt = "hello";
    long chunks = 4;
    long delay_ms = 0;
    long giant_bytes = 1 << 20;
    long initial_delay_ms = 0;

    std::vector<std::string> args(argv + 1, argv + argc);
    for (std::size_t i = 0; i < args.size(); ++i) {
        auto next = [&](long& out) {
            if (i + 1 < args.size()) out = std::strtol(args[++i].c_str(), nullptr, 10);
        };
        if (args[i] == "-p" && i + 1 < args.size()) {
            prompt = args[++i];
        } else if (args[i] == "--scenario" && i + 1 < args.size()) {
            scenario = args[++i];
        } else if (args[i] == "--chunks") {
            next(chunks);
        } else if (args[i] == "--delay-ms") {
            next(delay_ms);
        } else if (args[i] == "--giant-bytes") {
            next(giant_bytes);
        } else if (args[i] == "--initial-delay-ms") {
            next(initial_delay_ms);
        } else if (args[i] == "--output-format" && i + 1 < args.size()) {
            ++i;  // swallow value, claude-compat
        }
        // Anything else: swallowed, claude-compat.
    }

    const auto nap = [&] {
        if (delay_ms > 0) {
            std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
        }
    };

    if (initial_delay_ms > 0) {
        std::this_thread::sleep_for(
            std::chrono::milliseconds(initial_delay_ms));
    }

    emit_init();

    if (scenario == "echo") {
        emit_text(prompt);
        emit_result(false);
    } else if (scenario == "chunks") {
        if (chunks < 1) chunks = 1;
        std::size_t n = prompt.size();
        std::size_t per = n / static_cast<std::size_t>(chunks);
        if (per == 0) per = 1;
        for (std::size_t off = 0; off < n; off += per) {
            emit_text(prompt.substr(off, per));
            nap();
        }
        emit_result(false);
    } else if (scenario == "tool_storm") {
        for (long i = 0; i < chunks; ++i) {
            emit_tool("Bash", "git status --porcelain # storm " +
                              std::to_string(i));
            nap();
        }
        emit_text("storm complete: " + prompt);
        emit_result(false);
    } else if (scenario == "giant_chunk") {
        emit_text(std::string(static_cast<std::size_t>(giant_bytes), 'x'));
        emit_result(false);
    } else if (scenario == "die_before_result") {
        emit_text("about to die: " + prompt);
        return 3;
    } else if (scenario == "hang") {
        for (;;) std::this_thread::sleep_for(std::chrono::seconds(3600));
    } else if (scenario == "utf8_split") {
        // Pipe-level split: one assistant line delivered in two raw writes
        // whose boundary lands inside the 4-byte lion. The reader must frame
        // on '\n', never on write boundaries.
        const std::string lion = "\xF0\x9F\xA6\x81";
        const std::string line =
            R"({"type":"assistant","message":{"content":[{"type":"text",)"
            R"("text":"roar )" + lion + R"( savanna"}]}})";
        std::size_t cut = line.find(lion) + 2;  // two bytes into the lion
        std::fwrite(line.data(), 1, cut, stdout);
        std::fflush(stdout);
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
        std::fwrite(line.data() + cut, 1, line.size() - cut, stdout);
        std::fputc('\n', stdout);
        std::fflush(stdout);
        // Chunk-level split: "elephant" with acute accents, cut inside the
        // 2-byte e-acute. Each TEXT payload alone is invalid UTF-8; client
        // concatenation restores the byte sequence.
        const std::string eleph = " \xC3\xA9l\xC3\xA9phant";
        std::size_t mid = eleph.find('\xC3', 2) + 1;  // inside second e-acute
        emit_text(eleph.substr(0, mid));
        emit_text(eleph.substr(mid));
        emit_result(false);
    } else if (scenario == "stderr_noise") {
        // Loud agent: stderr is a separate channel savannahd inherits
        // rather than pipes. Each blob exceeds pipe capacity (64 KiB), so
        // if savannahd ever pipes stderr without draining it, this child
        // blocks mid-write and the scenario times out instead of passing.
        const std::string blob(128 * 1024, '#');
        for (int i = 0; i < 2; ++i) {
            std::fwrite(blob.data(), 1, blob.size(), stderr);
            std::fflush(stderr);
            emit_text("still fine " + std::to_string(i) + " ");
            nap();
        }
        emit_result(false);
    } else if (scenario == "bad_json") {
        emit_text("valid before garbage");
        emit("this is not json {{{");
        emit(R"({"type":"unknown_future_thing","x":1})");
        emit_text("valid after garbage: " + prompt);
        emit_result(false);
    } else {
        std::fprintf(stderr, "fake-claude: unknown scenario '%s'\n",
                     scenario.c_str());
        return 2;
    }
    return 0;
}

// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// Live schema drift check. Runs the REAL claude CLI once (burns a few
// tokens) through the same AgentProcess + ChunkMapper path savannahd uses,
// and asserts the pinned stream-json subset still holds: assistant text
// blocks arrive as TEXT chunks and the result event carries
// total_cost_usd / duration_ms / num_turns / is_error.
//
// Gated on SAVANNAH_LIVE=1 and never run in CI (exit 77 = ctest skip
// otherwise). If the CLI drifts, this fails while every fake-claude test
// stays green; that split is the whole point.

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "../src/agent_process.hpp"
#include "../src/json.hpp"
#include "../src/stream_json.hpp"

static int g_failures = 0;

#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                               \
        }                                                               \
    } while (0)

int main() {
    const char* live = std::getenv("SAVANNAH_LIVE");
    if (!live || std::string(live) != "1") {
        std::printf("test_live_schema: skipped (set SAVANNAH_LIVE=1 to run "
                    "against the real claude CLI; burns tokens)\n");
        return 77;  // ctest SKIP_RETURN_CODE
    }

    // Same argv shape savannahd builds, minimal turn budget.
    std::vector<std::string> argv = {
        "claude", "-p", "Reply with exactly the word OK and nothing else.",
        "--output-format", "stream-json", "--verbose", "--max-turns", "1"};

    std::string text;
    std::vector<std::string> results;
    std::atomic<bool> cancel{false};
    savannah::AgentProcess proc;
    auto outcome = proc.run(argv, /*timeout_ms=*/120000, cancel,
                            [&](const savannah::Chunk& c) {
                                if (c.kind == savannah::ChunkKind::Text)
                                    text += c.payload;
                                if (c.kind == savannah::ChunkKind::Result)
                                    results.push_back(c.payload);
                            });

    CHECK(outcome.spawned);  // claude not on PATH is a loud failure
    CHECK(!outcome.timed_out);
    CHECK(outcome.exit_code == 0);
    CHECK(outcome.saw_result);
    CHECK(outcome.malformed_lines == 0);  // every line parsed as JSON

    CHECK(text.find("OK") != std::string::npos);
    CHECK(results.size() == 1);

    if (results.size() == 1) {
        auto trailer = savannah::json::parse(results[0]);
        // Null fields mean the CLI renamed or dropped them: that is drift.
        CHECK(!trailer["cost_usd"].is_null());
        CHECK(trailer["cost_usd"].as_number(-1) > 0);
        CHECK(trailer["duration_ms"].as_number(-1) > 0);
        CHECK(trailer["num_turns"].as_number(-1) >= 1);
        CHECK(trailer["is_error"].as_bool(true) == false);
    }

    if (g_failures == 0) {
        std::printf("test_live_schema: live CLI matches the pinned subset\n");
    }
    return g_failures == 0 ? 0 : 1;
}

// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// Unit tests: stream-json → AgentChunk mapping, including the pathological
// inputs fake-claude can produce.

#include "../src/stream_json.hpp"

#include <cstdio>
#include <string>
#include <vector>

using savannah::Chunk;
using savannah::ChunkKind;
using savannah::ChunkMapper;

static int g_failures = 0;

#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                               \
        }                                                               \
    } while (0)

struct Collector {
    std::vector<Chunk> chunks;
    ChunkMapper::Sink sink() {
        return [this](const Chunk& c) { chunks.push_back(c); };
    }
};

static void test_text_and_result() {
    Collector col;
    ChunkMapper m(col.sink());
    m.feed_line(R"({"type":"system","subtype":"init","session_id":"s"})");
    m.feed_line(
        R"({"type":"assistant","message":{"content":[{"type":"text","text":"hello "},{"type":"text","text":"world"}]}})");
    m.feed_line(
        R"({"type":"result","total_cost_usd":0.001,"duration_ms":50,"num_turns":1,"is_error":false})");
    CHECK(col.chunks.size() == 3);
    CHECK(col.chunks[0].kind == ChunkKind::Text);
    CHECK(col.chunks[0].payload == "hello ");
    CHECK(col.chunks[1].payload == "world");
    CHECK(col.chunks[2].kind == ChunkKind::Result);
    CHECK(col.chunks[2].payload.find("\"is_error\":false") != std::string::npos);
    CHECK(col.chunks[2].payload.find("\"cost_usd\":0.001") != std::string::npos);
    CHECK(m.saw_result());
    CHECK(m.malformed_lines() == 0);
}

static void test_tool_events() {
    Collector col;
    ChunkMapper m(col.sink());
    m.feed_line(
        R"({"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"git status"}}]}})");
    m.feed_line(
        R"({"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"/etc/hosts"}}]}})");
    m.feed_line(
        R"({"type":"assistant","message":{"content":[{"type":"tool_use","name":"Mystery","input":{"weird":123}}]}})");
    CHECK(col.chunks.size() == 3);
    CHECK(col.chunks[0].kind == ChunkKind::ToolEvent);
    CHECK(col.chunks[0].payload == "Bash: git status");
    CHECK(col.chunks[1].payload == "Read: /etc/hosts");
    CHECK(col.chunks[2].payload == "Mystery");  // no string arg to summarize

    // Long tool input gets truncated.
    std::string big(500, 'y');
    m.feed_line(
        R"({"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":")" +
        big + R"("}}]}})");
    CHECK(col.chunks.back().payload.size() <= ChunkMapper::kToolSummaryMax);
    CHECK(col.chunks.back().payload.substr(
              col.chunks.back().payload.size() - 3) == "...");
}

static void test_garbage_and_unknown() {
    Collector col;
    ChunkMapper m(col.sink());
    m.feed_line("");
    m.feed_line("   ");
    m.feed_line("this is not json {{{");
    m.feed_line(R"({"type":"unknown_future_thing","x":1})");
    m.feed_line(R"([1,2,3])");  // valid JSON, wrong shape
    m.feed_line(R"({"type":"user","message":{"content":[]}})");
    CHECK(col.chunks.empty());
    CHECK(m.malformed_lines() == 2);  // garbage + wrong-shape
    CHECK(!m.saw_result());
}

static void test_error_result_and_synthetic() {
    Collector col;
    ChunkMapper m(col.sink());
    m.feed_line(
        R"({"type":"result","total_cost_usd":0.2,"duration_ms":9,"num_turns":3,"is_error":true})");
    CHECK(m.saw_result());
    CHECK(col.chunks.back().payload.find("\"is_error\":true") !=
          std::string::npos);

    std::string synth = ChunkMapper::synthetic_error_result("timeout", -1);
    CHECK(synth.find("\"is_error\":true") != std::string::npos);
    CHECK(synth.find("\"reason\":\"timeout\"") != std::string::npos);
    CHECK(synth.find("\"exit_code\":-1") != std::string::npos);
}

static void test_empty_text_skipped() {
    Collector col;
    ChunkMapper m(col.sink());
    m.feed_line(
        R"({"type":"assistant","message":{"content":[{"type":"text","text":""}]}})");
    CHECK(col.chunks.empty());
}

int main() {
    test_text_and_result();
    test_tool_events();
    test_garbage_and_unknown();
    test_error_result_and_synthetic();
    test_empty_text_skipped();
    if (g_failures == 0) std::printf("test_stream_json: all passed\n");
    return g_failures == 0 ? 0 : 1;
}

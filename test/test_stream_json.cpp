// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// Unit tests: stream-json → AgentChunk mapping, including the pathological
// inputs fake-claude can produce.

#include "stream_json.hpp"
#include "test_support.hpp"

#include <cstdio>
#include <string>
#include <vector>

using savannah::Chunk;
using savannah::ChunkKind;
using savannah::ChunkMapper;

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
    CHECK(ChunkMapper::result_ok(col.chunks[2].payload));
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

// result_ok must agree with the substring test it replaced
// (`"is_error":false` somewhere in the trailer) on every trailer this code
// produces: the mapper's, with and without a subtype, and the synthetic one.
static void test_result_ok() {
    auto substring_ok = [](const std::string& trailer) {
        return trailer.find("\"is_error\":false") != std::string::npos;
    };
    auto mapped = [](const std::string& result_line) {
        Collector col;
        ChunkMapper m(col.sink());
        m.feed_line(result_line);
        return col.chunks.empty() ? std::string() : col.chunks.back().payload;
    };
    const std::vector<std::string> trailers = {
        // fake-claude's two result lines, through the mapper.
        mapped(R"({"type":"result","subtype":"success","total_cost_usd":0.00042,"duration_ms":1234,"num_turns":1,"is_error":false})"),
        mapped(R"({"type":"result","subtype":"error_max_turns","total_cost_usd":0.00042,"duration_ms":1234,"num_turns":1,"is_error":true})"),
        // No subtype, and no is_error at all (the mapper writes false).
        mapped(R"({"type":"result","total_cost_usd":0.2,"duration_ms":9,"num_turns":3,"is_error":true})"),
        mapped(R"({"type":"result","total_cost_usd":0.2,"duration_ms":9,"num_turns":3})"),
        // Every reason savannahd and the task supervisor synthesize.
        ChunkMapper::synthetic_error_result("busy", 0),
        ChunkMapper::synthetic_error_result("spawn failed", -1),
        ChunkMapper::synthetic_error_result("timeout", 137),
        ChunkMapper::synthetic_error_result("cancelled", 137),
        ChunkMapper::synthetic_error_result("died before result", 3),
        ChunkMapper::synthetic_error_result("no such task", 0),
    };
    for (const auto& t : trailers) {
        CHECK(!t.empty());
        CHECK(ChunkMapper::result_ok(t) == substring_ok(t));
    }
    CHECK(ChunkMapper::result_ok(trailers[0]));
    CHECK(!ChunkMapper::result_ok(trailers[1]));
    CHECK(!ChunkMapper::result_ok(trailers[2]));
    CHECK(ChunkMapper::result_ok(trailers[3]));

    // Not a trailer at all: never ok, never a throw.
    CHECK(!ChunkMapper::result_ok(""));
    CHECK(!ChunkMapper::result_ok("not json"));
    CHECK(!ChunkMapper::result_ok("[false]"));
    CHECK(!ChunkMapper::result_ok(R"({"reason":"x"})"));
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
    test_result_ok();
    test_empty_text_skipped();
    return savannah::test::finish("test_stream_json");
}

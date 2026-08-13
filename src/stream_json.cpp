// MIT License
// Copyright (c) 2026 Dennis B Jones

#include "stream_json.hpp"

#include "json.hpp"

namespace savannah {

namespace {

// Pick the most human-useful string out of a tool_use input object:
// prefer well-known keys, fall back to the first string member.
std::string summarize_tool_input(const json::Value& input) {
    static const char* preferred[] = {"command", "file_path", "path",
                                      "pattern", "url", "query", "prompt"};
    for (const char* key : preferred) {
        const auto& v = input[key];
        if (v.is_string() && !v.as_string().empty()) return v.as_string();
    }
    for (const auto& [k, v] : input.as_object()) {
        (void)k;
        if (v.is_string() && !v.as_string().empty()) return v.as_string();
    }
    return "";
}

std::string truncate(std::string s, std::size_t max) {
    if (s.size() <= max) return s;
    s.resize(max - 3);
    s += "...";
    return s;
}

}  // namespace

void ChunkMapper::feed_line(const std::string& line) {
    // Cheap skip for blank lines before paying for a parse.
    bool blank = true;
    for (char c : line) {
        if (c != ' ' && c != '\t' && c != '\r') { blank = false; break; }
    }
    if (blank) return;

    json::Value doc;
    try {
        doc = json::parse(line);
    } catch (const json::ParseError&) {
        ++malformed_;
        return;
    }
    if (!doc.is_object()) { ++malformed_; return; }

    const std::string& type = doc["type"].as_string();

    if (type == "assistant") {
        const auto& content = doc["message"]["content"];
        for (const auto& block : content.as_array()) {
            const std::string& btype = block["type"].as_string();
            if (btype == "text") {
                const std::string& text = block["text"].as_string();
                if (!text.empty()) {
                    sink_(Chunk{ChunkKind::Text, text});
                }
            } else if (btype == "tool_use") {
                std::string name = block["name"].as_string();
                if (name.empty()) name = "unknown-tool";
                std::string summary = summarize_tool_input(block["input"]);
                std::string payload =
                    summary.empty() ? name : name + ": " + summary;
                sink_(Chunk{ChunkKind::ToolEvent,
                            truncate(std::move(payload), kToolSummaryMax)});
            }
        }
        return;
    }

    if (type == "result") {
        const json::Value& subtype = doc["subtype"];
        result_subtype_ = subtype.is_null() ? std::string() : subtype.as_string();
        json::Object trailer;
        trailer.emplace("cost_usd", doc["total_cost_usd"]);
        trailer.emplace("duration_ms", doc["duration_ms"]);
        trailer.emplace("num_turns", doc["num_turns"]);
        trailer.emplace("is_error",
                        json::Value(doc["is_error"].as_bool(false)));
        // subtype distinguishes a clean finish ("success") from hitting the
        // per-invocation turn cap ("error_max_turns", continuable) or a real
        // failure, so a reader is not left guessing at a bare is_error flag.
        if (!subtype.is_null()) trailer.emplace("subtype", subtype);
        sink_(Chunk{ChunkKind::Result, json::dump(json::Value(trailer))});
        saw_result_ = true;
        return;
    }

    // system, user, anything future: ignored on purpose.
}

std::string ChunkMapper::synthetic_error_result(const std::string& reason,
                                                int exit_code) {
    json::Object trailer;
    trailer.emplace("is_error", json::Value(true));
    trailer.emplace("reason", json::Value(reason));
    trailer.emplace("exit_code",
                    json::Value(static_cast<double>(exit_code)));
    return json::dump(json::Value(trailer));
}

}  // namespace savannah

// MIT License
// Copyright (c) 2026 Dennis B Jones

#include "config.hpp"

#include <cctype>
#include <fstream>
#include <sstream>

namespace savannah {

namespace {

// One line of the lexing state: we parse line-at-a-time because the grammar
// has no multi-line constructs. Keeps error reporting trivially precise.
struct LineCursor {
    const std::string& s;
    std::size_t pos = 0;
    int line_no;

    LineCursor(const std::string& str, int line) : s(str), line_no(line) {}

    void skip_ws() {
        while (pos < s.size() && (s[pos] == ' ' || s[pos] == '\t')) ++pos;
    }
    bool at_end_or_comment() {
        skip_ws();
        return pos >= s.size() || s[pos] == '#';
    }
    char peek() const { return pos < s.size() ? s[pos] : '\0'; }
    [[noreturn]] void fail(const std::string& msg) const {
        throw ConfigError(msg, line_no);
    }
};

std::string parse_bare_key(LineCursor& c) {
    c.skip_ws();
    std::size_t start = c.pos;
    while (c.pos < c.s.size() &&
           (std::isalnum(static_cast<unsigned char>(c.s[c.pos])) != 0 ||
            c.s[c.pos] == '_' || c.s[c.pos] == '-')) {
        ++c.pos;
    }
    if (c.pos == start) c.fail("expected key");
    return c.s.substr(start, c.pos - start);
}

std::string parse_quoted_string(LineCursor& c) {
    if (c.peek() != '"') c.fail("expected '\"'");
    ++c.pos;
    std::string out;
    while (c.pos < c.s.size() && c.s[c.pos] != '"') {
        char ch = c.s[c.pos];
        if (ch == '\\') {
            ++c.pos;
            if (c.pos >= c.s.size()) c.fail("dangling escape");
            switch (c.s[c.pos]) {
                case 'n': out.push_back('\n'); break;
                case 't': out.push_back('\t'); break;
                case '"': out.push_back('"'); break;
                case '\\': out.push_back('\\'); break;
                default: c.fail("unsupported escape in string");
            }
        } else {
            out.push_back(ch);
        }
        ++c.pos;
    }
    if (c.pos >= c.s.size()) c.fail("unterminated string");
    ++c.pos;  // closing quote
    return out;
}

Config::Value parse_value(LineCursor& c) {
    c.skip_ws();
    char ch = c.peek();

    if (ch == '"') {
        return parse_quoted_string(c);
    }

    if (ch == '[') {
        ++c.pos;
        std::vector<std::string> arr;
        for (;;) {
            c.skip_ws();
            if (c.peek() == ']') { ++c.pos; break; }
            arr.push_back(parse_quoted_string(c));
            c.skip_ws();
            if (c.peek() == ',') { ++c.pos; continue; }
            if (c.peek() == ']') { ++c.pos; break; }
            c.fail("expected ',' or ']' in array");
        }
        return arr;
    }

    if (ch == 't' || ch == 'f') {
        if (c.s.compare(c.pos, 4, "true") == 0) { c.pos += 4; return true; }
        if (c.s.compare(c.pos, 5, "false") == 0) { c.pos += 5; return false; }
        c.fail("expected true or false");
    }

    if (ch == '-' || std::isdigit(static_cast<unsigned char>(ch)) != 0) {
        std::size_t start = c.pos;
        if (ch == '-') ++c.pos;
        while (c.pos < c.s.size() &&
               std::isdigit(static_cast<unsigned char>(c.s[c.pos])) != 0) {
            ++c.pos;
        }
        if (c.pos == start || (c.s[start] == '-' && c.pos == start + 1)) {
            c.fail("expected integer");
        }
        try {
            return static_cast<std::int64_t>(
                std::stoll(c.s.substr(start, c.pos - start)));
        } catch (const std::out_of_range&) {
            c.fail("integer out of range");
        }
    }

    c.fail("expected value (string, integer, bool, or string array)");
}

}  // namespace

Config Config::parse_string(const std::string& text) {
    Config cfg;
    std::istringstream in(text);
    std::string line;
    std::string table;
    int line_no = 0;

    while (std::getline(in, line)) {
        ++line_no;
        if (!line.empty() && line.back() == '\r') line.pop_back();
        LineCursor c(line, line_no);
        if (c.at_end_or_comment()) continue;

        if (c.peek() == '[') {
            ++c.pos;
            table = parse_bare_key(c);
            c.skip_ws();
            if (c.peek() != ']') c.fail("expected ']' after table name");
            ++c.pos;
            if (!c.at_end_or_comment()) c.fail("trailing junk after table header");
            continue;
        }

        std::string key = parse_bare_key(c);
        c.skip_ws();
        if (c.peek() != '=') c.fail("expected '=' after key");
        ++c.pos;
        Value v = parse_value(c);
        if (!c.at_end_or_comment()) c.fail("trailing junk after value");

        std::string full = table.empty() ? key : table + "." + key;
        if (cfg.values_.count(full) > 0) c.fail("duplicate key: " + full);
        cfg.values_.emplace(std::move(full), std::move(v));
    }
    return cfg;
}

Config Config::parse_file(const std::string& path) {
    std::ifstream f(path);
    if (!f) throw ConfigError("cannot open " + path, 0);
    std::ostringstream ss;
    ss << f.rdbuf();
    return parse_string(ss.str());
}

std::optional<std::string> Config::get_string(const std::string& key) const {
    auto it = values_.find(key);
    if (it == values_.end()) return std::nullopt;
    if (const auto* p = std::get_if<std::string>(&it->second)) return *p;
    return std::nullopt;
}

std::optional<std::int64_t> Config::get_int(const std::string& key) const {
    auto it = values_.find(key);
    if (it == values_.end()) return std::nullopt;
    if (const auto* p = std::get_if<std::int64_t>(&it->second)) return *p;
    return std::nullopt;
}

std::optional<bool> Config::get_bool(const std::string& key) const {
    auto it = values_.find(key);
    if (it == values_.end()) return std::nullopt;
    if (const auto* p = std::get_if<bool>(&it->second)) return *p;
    return std::nullopt;
}

std::optional<std::vector<std::string>> Config::get_string_array(
    const std::string& key) const {
    auto it = values_.find(key);
    if (it == values_.end()) return std::nullopt;
    if (const auto* p = std::get_if<std::vector<std::string>>(&it->second)) {
        return *p;
    }
    return std::nullopt;
}

NodeConfig NodeConfig::from(const Config& cfg) {
    NodeConfig nc;

    auto name = cfg.get_string("node.name");
    if (!name || name->empty()) throw ConfigError("node.name is required", 0);
    nc.name = *name;

    auto ws = cfg.get_string("node.workspace");
    if (!ws || ws->empty()) throw ConfigError("node.workspace is required", 0);
    nc.workspace = *ws;

    if (auto tags = cfg.get_string_array("node.tags")) nc.tags = *tags;

    if (auto cmd = cfg.get_string("agent.cmd")) nc.agent_cmd = *cmd;
    if (auto args = cfg.get_string_array("agent.args")) nc.agent_args = *args;
    if (auto at = cfg.get_string_array("agent.allowed_tools")) {
        nc.allowed_tools = *at;
    }
    if (auto mt = cfg.get_int("agent.max_turns")) nc.max_turns = *mt;
    if (auto tm = cfg.get_int("agent.timeout_ms")) nc.timeout_ms = *tm;
    if (nc.timeout_ms <= 0) throw ConfigError("agent.timeout_ms must be > 0", 0);

    if (auto kf = cfg.get_string("mesh.hmac_key_file")) nc.hmac_key_file = *kf;
    if (auto mc = cfg.get_int("mesh.max_concurrent")) nc.max_concurrent = *mc;
    if (nc.max_concurrent != 1) {
        throw ConfigError("mesh.max_concurrent must be 1 in v1 (single-flight)", 0);
    }

    return nc;
}

}  // namespace savannah

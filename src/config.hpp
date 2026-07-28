// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// config.hpp — hand-written TOML-subset parser + NodeConfig.
//
// Supported subset (deliberately small; this is a config file, not a document
// format): [table] headers, key = value with value one of: "string",
// integer, true/false, ["array","of","strings"]. Comments with #. That's it.
// No nested tables, no dotted keys, no dates, no floats, no multiline strings.
// Wanting more than this from a node config file is a design smell.

#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>

namespace savannah {

class ConfigError : public std::runtime_error {
public:
    ConfigError(const std::string& msg, int line)
        : std::runtime_error("config:" + std::to_string(line) + ": " + msg),
          line_(line) {}
    int line() const { return line_; }

private:
    int line_;
};

/// Parsed key/value store. Keys are "table.key" (or bare "key" before any
/// table header). Values are string, int64, bool, or vector<string>.
class Config {
public:
    using Value = std::variant<std::string, std::int64_t, bool,
                               std::vector<std::string>>;

    /// Parse TOML-subset text. Throws ConfigError with a line number.
    static Config parse_string(const std::string& text);

    /// Parse a file. Throws ConfigError (line 0) if unreadable.
    static Config parse_file(const std::string& path);

    bool has(const std::string& key) const { return values_.count(key) > 0; }

    std::optional<std::string> get_string(const std::string& key) const;
    std::optional<std::int64_t> get_int(const std::string& key) const;
    std::optional<bool> get_bool(const std::string& key) const;
    std::optional<std::vector<std::string>> get_string_array(
        const std::string& key) const;

    std::size_t size() const { return values_.size(); }

private:
    std::map<std::string, Value> values_;
};

/// Validated savannahd node configuration.
struct NodeConfig {
    // [node]
    std::string name;                     // required
    std::vector<std::string> tags;        // optional
    std::string workspace;                // required

    // [agent]
    std::string agent_cmd = "claude";     // binary to run
    std::vector<std::string> agent_args;  // extra args, prepended before ours
    std::vector<std::string> allowed_tools;
    std::int64_t max_turns = 25;
    std::int64_t timeout_ms = 300000;

    // [mesh]
    std::string hmac_key_file;            // optional in Phase 1 (pipes)
    std::int64_t max_concurrent = 1;      // v1: must be 1

    /// Build from parsed Config. Throws ConfigError on missing/invalid fields.
    static NodeConfig from(const Config& cfg);
};

/// Read an HMAC key file: raw bytes, one trailing newline tolerated.
/// Throws ConfigError if unreadable or shorter than 32 bytes (song's
/// kMinKeySize; a weaker mesh key is a misconfiguration, not a choice).
std::string load_hmac_key(const std::string& path);

}  // namespace savannah

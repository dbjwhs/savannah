// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// Unit tests: TOML-subset config parser. Plain assert-style harness,
// one test function per behavior, main() runs them all.

#include "../src/config.hpp"

#include <cassert>
#include <cstdio>
#include <string>

using savannah::Config;
using savannah::ConfigError;
using savannah::NodeConfig;

static int g_failures = 0;

#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++g_failures;                                               \
        }                                                               \
    } while (0)

#define CHECK_THROWS(expr)                                              \
    do {                                                                \
        bool threw = false;                                             \
        try { (void)(expr); } catch (const ConfigError&) { threw = true; } \
        CHECK(threw);                                                   \
    } while (0)

static void test_basic_types() {
    auto cfg = Config::parse_string(R"(
# a comment
title = "hello world"
count = 42
neg = -7
flag = true
off = false
list = ["a", "b", "c"]
empty = []
)");
    CHECK(cfg.get_string("title") == std::string("hello world"));
    CHECK(cfg.get_int("count") == 42);
    CHECK(cfg.get_int("neg") == -7);
    CHECK(cfg.get_bool("flag") == true);
    CHECK(cfg.get_bool("off") == false);
    auto list = cfg.get_string_array("list");
    CHECK(list && list->size() == 3 && (*list)[1] == "b");
    auto empty = cfg.get_string_array("empty");
    CHECK(empty && empty->empty());
}

static void test_tables_and_keys() {
    auto cfg = Config::parse_string(R"(
[node]
name = "m4-studio"
tags = ["cpp", "zfs"]

[agent]   # trailing comment on header
cmd = "claude"
)");
    CHECK(cfg.get_string("node.name") == std::string("m4-studio"));
    CHECK(cfg.get_string("agent.cmd") == std::string("claude"));
    CHECK(!cfg.has("name"));  // keys are table-scoped
}

static void test_string_escapes() {
    auto cfg = Config::parse_string(
        "s = \"line\\nbreak\\ttab \\\"quoted\\\" back\\\\slash\"\n");
    CHECK(cfg.get_string("s") ==
          std::string("line\nbreak\ttab \"quoted\" back\\slash"));
}

static void test_wrong_type_returns_nullopt() {
    auto cfg = Config::parse_string("x = 5\n");
    CHECK(!cfg.get_string("x").has_value());
    CHECK(!cfg.get_bool("x").has_value());
    CHECK(cfg.get_int("x").has_value());
    CHECK(!cfg.get_int("missing").has_value());
}

static void test_errors() {
    CHECK_THROWS(Config::parse_string("key\n"));                 // no '='
    CHECK_THROWS(Config::parse_string("key = \n"));              // no value
    CHECK_THROWS(Config::parse_string("key = \"unterminated\n"));
    CHECK_THROWS(Config::parse_string("key = [\"a\" \"b\"]\n")); // no comma
    CHECK_THROWS(Config::parse_string("[table\n"));              // no ']'
    CHECK_THROWS(Config::parse_string("a = 1\na = 2\n"));        // duplicate
    CHECK_THROWS(Config::parse_string("a = 1 trailing\n"));      // junk
    CHECK_THROWS(Config::parse_string("a = tru\n"));             // bad literal
    CHECK_THROWS(Config::parse_string(
        "a = 99999999999999999999999999\n"));                    // overflow
    // Line numbers are reported.
    try {
        Config::parse_string("ok = 1\nbroken\n");
        CHECK(false);
    } catch (const ConfigError& e) {
        CHECK(e.line() == 2);
    }
}

static void test_node_config() {
    auto cfg = Config::parse_string(R"(
[node]
name = "test-node"
workspace = "/tmp/ws"
tags = ["reviewer"]

[agent]
cmd = "./fake-claude"
args = ["--scenario", "echo"]
allowed_tools = ["Read", "Grep"]
max_turns = 5
timeout_ms = 10000

[mesh]
max_concurrent = 1
)");
    auto nc = NodeConfig::from(cfg);
    CHECK(nc.name == "test-node");
    CHECK(nc.workspace == "/tmp/ws");
    CHECK(nc.agent_cmd == "./fake-claude");
    CHECK(nc.agent_args.size() == 2);
    CHECK(nc.allowed_tools.size() == 2);
    CHECK(nc.max_turns == 5);
    CHECK(nc.timeout_ms == 10000);

    // Missing required fields throw.
    CHECK_THROWS(NodeConfig::from(Config::parse_string("[node]\nname = \"x\"\n")));
    CHECK_THROWS(NodeConfig::from(Config::parse_string(
        "[node]\nname = \"x\"\nworkspace = \"/w\"\n[mesh]\nmax_concurrent = 2\n")));
    // Defaults hold.
    auto min = NodeConfig::from(Config::parse_string(
        "[node]\nname = \"x\"\nworkspace = \"/w\"\n"));
    CHECK(min.agent_cmd == "claude");
    CHECK(min.max_turns == 25);
    CHECK(min.timeout_ms == 300000);
    CHECK(min.max_concurrent == 1);
}

int main() {
    test_basic_types();
    test_tables_and_keys();
    test_string_escapes();
    test_wrong_type_returns_nullopt();
    test_errors();
    test_node_config();
    if (g_failures == 0) std::printf("test_config: all passed\n");
    return g_failures == 0 ? 0 : 1;
}

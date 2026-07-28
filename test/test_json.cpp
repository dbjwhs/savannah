// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// Unit tests: minimal JSON parser.

#include "../src/json.hpp"

#include <cstdio>
#include <string>

using namespace savannah::json;

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
        try { (void)(expr); } catch (const ParseError&) { threw = true; } \
        CHECK(threw);                                                   \
    } while (0)

static void test_scalars() {
    CHECK(parse("null").is_null());
    CHECK(parse("true").as_bool() == true);
    CHECK(parse("false").as_bool() == false);
    CHECK(parse("42").as_number() == 42.0);
    CHECK(parse("-3.5").as_number() == -3.5);
    CHECK(parse("1e3").as_number() == 1000.0);
    CHECK(parse("\"hi\"").as_string() == "hi");
    CHECK(parse("  \"pad\"  ").as_string() == "pad");
}

static void test_structures() {
    auto v = parse(R"({"a":1,"b":[true,null,"x"],"c":{"d":"deep"}})");
    CHECK(v.is_object());
    CHECK(v["a"].as_number() == 1.0);
    CHECK(v["b"].as_array().size() == 3);
    CHECK(v["b"].as_array()[2].as_string() == "x");
    CHECK(v["c"]["d"].as_string() == "deep");
    // Missing keys are safe nulls, chainable.
    CHECK(v["missing"]["also missing"].is_null());
    CHECK(v["missing"].as_string().empty());
    CHECK(parse("[]").as_array().empty());
    CHECK(parse("{}").as_object().empty());
}

static void test_string_escapes() {
    CHECK(parse(R"("a\nb\tc\"d\\e\/f")").as_string() == "a\nb\tc\"d\\e/f");
    CHECK(parse(R"("A")").as_string() == "A");
    CHECK(parse(R"("é")").as_string() == "\xc3\xa9");          // é
    CHECK(parse(R"("中")").as_string() == "\xe4\xb8\xad");      // 中
    CHECK(parse(R"("😀")").as_string() ==
          "\xf0\x9f\x98\x80");                                       // emoji
    CHECK_THROWS(parse(R"("\ud83d")"));       // lone high surrogate
    CHECK_THROWS(parse(R"("\ude00")"));       // lone low surrogate
    CHECK_THROWS(parse(R"("\q")"));           // bad escape
}

static void test_errors() {
    CHECK_THROWS(parse(""));
    CHECK_THROWS(parse("{"));
    CHECK_THROWS(parse("[1,]"));
    CHECK_THROWS(parse("{\"a\":}"));
    CHECK_THROWS(parse("\"unterminated"));
    CHECK_THROWS(parse("tru"));
    CHECK_THROWS(parse("{} trailing"));
}

static void test_dump() {
    CHECK(dump(parse(R"({"b":true,"n":null,"s":"x","v":3})")) ==
          R"({"b":true,"n":null,"s":"x","v":3})");
    CHECK(dump(Value(0.00042)) == "0.00042");
    CHECK(dump(Value(1234.0)) == "1234");
    CHECK(dump(Value(std::string("a\"b\nc"))) == R"("a\"b\nc")");
    // Round-trip a realistic result trailer.
    std::string trailer =
        R"({"cost_usd":0.00042,"duration_ms":1234,"is_error":false,"num_turns":1})";
    CHECK(dump(parse(trailer)) == trailer);
}

int main() {
    test_scalars();
    test_structures();
    test_string_escapes();
    test_errors();
    test_dump();
    if (g_failures == 0) std::printf("test_json: all passed\n");
    return g_failures == 0 ? 0 : 1;
}

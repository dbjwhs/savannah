// MIT License
// Copyright (c) 2026 Dennis B Jones

#include "json.hpp"

#include <cctype>
#include <cmath>
#include <cstdio>

namespace savannah::json {

const Value& Value::operator[](const std::string& key) const {
    static const Value null_value;
    if (kind_ != Kind::Object || !obj_) return null_value;
    auto it = obj_->find(key);
    return it == obj_->end() ? null_value : it->second;
}

namespace {

class Parser {
public:
    explicit Parser(const std::string& text) : s_(text) {}

    Value parse_document() {
        Value v = parse_value();
        skip_ws();
        if (pos_ != s_.size()) fail("trailing characters");
        return v;
    }

private:
    const std::string& s_;
    std::size_t pos_ = 0;

    [[noreturn]] void fail(const std::string& msg) const {
        throw ParseError(msg, pos_);
    }

    void skip_ws() {
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
            else break;
        }
    }

    char peek() {
        if (pos_ >= s_.size()) fail("unexpected end of input");
        return s_[pos_];
    }

    void expect(char c) {
        if (peek() != c) fail(std::string("expected '") + c + "'");
        ++pos_;
    }

    bool consume_literal(const char* lit) {
        std::size_t n = std::char_traits<char>::length(lit);
        if (s_.compare(pos_, n, lit) == 0) { pos_ += n; return true; }
        return false;
    }

    Value parse_value() {
        skip_ws();
        char c = peek();
        switch (c) {
            case '{': return parse_object();
            case '[': return parse_array();
            case '"': return Value(parse_string());
            case 't':
                if (consume_literal("true")) return Value(true);
                fail("bad literal");
            case 'f':
                if (consume_literal("false")) return Value(false);
                fail("bad literal");
            case 'n':
                if (consume_literal("null")) return Value();
                fail("bad literal");
            default:  return parse_number();
        }
    }

    Value parse_object() {
        expect('{');
        Object obj;
        skip_ws();
        if (peek() == '}') { ++pos_; return Value(std::move(obj)); }
        for (;;) {
            skip_ws();
            std::string key = parse_string();
            skip_ws();
            expect(':');
            obj.emplace(std::move(key), parse_value());
            skip_ws();
            char c = peek();
            if (c == ',') { ++pos_; continue; }
            if (c == '}') { ++pos_; break; }
            fail("expected ',' or '}' in object");
        }
        return Value(std::move(obj));
    }

    Value parse_array() {
        expect('[');
        Array arr;
        skip_ws();
        if (peek() == ']') { ++pos_; return Value(std::move(arr)); }
        for (;;) {
            arr.push_back(parse_value());
            skip_ws();
            char c = peek();
            if (c == ',') { ++pos_; continue; }
            if (c == ']') { ++pos_; break; }
            fail("expected ',' or ']' in array");
        }
        return Value(std::move(arr));
    }

    unsigned parse_hex4() {
        unsigned v = 0;
        for (int i = 0; i < 4; ++i) {
            char c = peek();
            ++pos_;
            v <<= 4;
            if (c >= '0' && c <= '9') v |= static_cast<unsigned>(c - '0');
            else if (c >= 'a' && c <= 'f') v |= static_cast<unsigned>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= static_cast<unsigned>(c - 'A' + 10);
            else fail("bad \\u escape");
        }
        return v;
    }

    static void append_utf8(std::string& out, unsigned cp) {
        if (cp < 0x80) {
            out.push_back(static_cast<char>(cp));
        } else if (cp < 0x800) {
            out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else if (cp < 0x10000) {
            out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        }
    }

    std::string parse_string() {
        expect('"');
        std::string out;
        for (;;) {
            if (pos_ >= s_.size()) fail("unterminated string");
            char c = s_[pos_++];
            if (c == '"') break;
            if (c == '\\') {
                if (pos_ >= s_.size()) fail("dangling escape");
                char e = s_[pos_++];
                switch (e) {
                    case '"':  out.push_back('"'); break;
                    case '\\': out.push_back('\\'); break;
                    case '/':  out.push_back('/'); break;
                    case 'b':  out.push_back('\b'); break;
                    case 'f':  out.push_back('\f'); break;
                    case 'n':  out.push_back('\n'); break;
                    case 'r':  out.push_back('\r'); break;
                    case 't':  out.push_back('\t'); break;
                    case 'u': {
                        unsigned cp = parse_hex4();
                        if (cp >= 0xD800 && cp <= 0xDBFF) {
                            // High surrogate: require the low half.
                            if (pos_ + 1 < s_.size() && s_[pos_] == '\\' &&
                                s_[pos_ + 1] == 'u') {
                                pos_ += 2;
                                unsigned lo = parse_hex4();
                                if (lo < 0xDC00 || lo > 0xDFFF) {
                                    fail("bad low surrogate");
                                }
                                cp = 0x10000 + ((cp - 0xD800) << 10) +
                                     (lo - 0xDC00);
                            } else {
                                fail("lone high surrogate");
                            }
                        } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                            fail("lone low surrogate");
                        }
                        append_utf8(out, cp);
                        break;
                    }
                    default: fail("unsupported escape");
                }
            } else {
                out.push_back(c);
            }
        }
        return out;
    }

    Value parse_number() {
        std::size_t start = pos_;
        if (peek() == '-') ++pos_;
        while (pos_ < s_.size() &&
               (std::isdigit(static_cast<unsigned char>(s_[pos_])) != 0 ||
                s_[pos_] == '.' || s_[pos_] == 'e' || s_[pos_] == 'E' ||
                s_[pos_] == '+' || s_[pos_] == '-')) {
            ++pos_;
        }
        if (pos_ == start) fail("expected number");
        try {
            return Value(std::stod(s_.substr(start, pos_ - start)));
        } catch (const std::exception&) {
            fail("bad number");
        }
    }
};

void dump_string(std::string& out, const std::string& s) {
    out.push_back('"');
    for (char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x",
                                  static_cast<unsigned>(c) & 0xFF);
                    out += buf;
                } else {
                    out.push_back(c);
                }
        }
    }
    out.push_back('"');
}

void dump_value(std::string& out, const Value& v) {
    switch (v.kind()) {
        case Value::Kind::Null:   out += "null"; break;
        case Value::Kind::Bool:   out += v.as_bool() ? "true" : "false"; break;
        case Value::Kind::Number: {
            double d = v.as_number();
            double intpart;
            char buf[32];
            if (std::modf(d, &intpart) == 0.0 && std::fabs(d) < 1e15) {
                std::snprintf(buf, sizeof buf, "%lld",
                              static_cast<long long>(d));
            } else {
                std::snprintf(buf, sizeof buf, "%.6g", d);
            }
            out += buf;
            break;
        }
        case Value::Kind::String: dump_string(out, v.as_string()); break;
        case Value::Kind::Array: {
            out.push_back('[');
            bool first = true;
            for (const auto& e : v.as_array()) {
                if (!first) out.push_back(',');
                first = false;
                dump_value(out, e);
            }
            out.push_back(']');
            break;
        }
        case Value::Kind::Object: {
            out.push_back('{');
            bool first = true;
            for (const auto& [k, e] : v.as_object()) {
                if (!first) out.push_back(',');
                first = false;
                dump_string(out, k);
                out.push_back(':');
                dump_value(out, e);
            }
            out.push_back('}');
            break;
        }
    }
}

}  // namespace

Value parse(const std::string& text) {
    return Parser(text).parse_document();
}

std::string dump(const Value& v) {
    std::string out;
    dump_value(out, v);
    return out;
}

}  // namespace savannah::json

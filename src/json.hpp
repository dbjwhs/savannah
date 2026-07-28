// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// json.hpp — minimal hand-written JSON parser.
//
// Exists to read claude's stream-json lines and nothing else. Full value
// model (null, bool, number, string, array, object), UTF-8 pass-through,
// \uXXXX decoding (BMP + surrogate pairs). No serializer beyond the tiny
// compact writer needed for RESULT trailers. No SAX, no comments, no
// trailing commas: input is machine-generated.

#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace savannah::json {

class ParseError : public std::runtime_error {
public:
    ParseError(const std::string& msg, std::size_t offset)
        : std::runtime_error("json:" + std::to_string(offset) + ": " + msg) {}
};

class Value;
using Array = std::vector<Value>;
using Object = std::map<std::string, Value>;

class Value {
public:
    enum class Kind { Null, Bool, Number, String, Array, Object };

    Value() : kind_(Kind::Null) {}
    explicit Value(bool b) : kind_(Kind::Bool), bool_(b) {}
    explicit Value(double d) : kind_(Kind::Number), num_(d) {}
    explicit Value(std::string s) : kind_(Kind::String), str_(std::move(s)) {}
    explicit Value(Array a)
        : kind_(Kind::Array), arr_(std::make_shared<Array>(std::move(a))) {}
    explicit Value(Object o)
        : kind_(Kind::Object), obj_(std::make_shared<Object>(std::move(o))) {}

    Kind kind() const { return kind_; }
    bool is_null() const { return kind_ == Kind::Null; }
    bool is_object() const { return kind_ == Kind::Object; }
    bool is_array() const { return kind_ == Kind::Array; }
    bool is_string() const { return kind_ == Kind::String; }

    bool as_bool(bool fallback = false) const {
        return kind_ == Kind::Bool ? bool_ : fallback;
    }
    double as_number(double fallback = 0.0) const {
        return kind_ == Kind::Number ? num_ : fallback;
    }
    const std::string& as_string() const {
        static const std::string empty;
        return kind_ == Kind::String ? str_ : empty;
    }
    const Array& as_array() const {
        static const Array empty;
        return kind_ == Kind::Array && arr_ ? *arr_ : empty;
    }
    const Object& as_object() const {
        static const Object empty;
        return kind_ == Kind::Object && obj_ ? *obj_ : empty;
    }

    /// Object member access; returns Null value if absent or not an object.
    const Value& operator[](const std::string& key) const;

private:
    Kind kind_;
    bool bool_ = false;
    double num_ = 0.0;
    std::string str_;
    std::shared_ptr<Array> arr_;
    std::shared_ptr<Object> obj_;
};

/// Parse one JSON document. Throws ParseError. Trailing whitespace allowed;
/// trailing non-whitespace is an error.
Value parse(const std::string& text);

/// Compact serialization (for RESULT trailers). Strings are escaped;
/// numbers print with %.6g semantics, integers without decimals.
std::string dump(const Value& v);

}  // namespace savannah::json

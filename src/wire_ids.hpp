// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// wire_ids.hpp — service-id split for streaming.
//
// song dispatches by service id, and a stream dispatcher on an id shadows
// the unary dispatcher entirely (runtime.cpp checks stream first, returns).
// Convention from song's own backup example: streaming methods live on their
// own service id. songc cannot express this split yet (song wishlist), so
// the extra id lives here, adjacent to the generated header.

#pragma once

#include <song/types.hpp>

namespace savannah_wire {

// Generated: kService_AgentNode = 1 (unary: info, cancel, status).
// Manual:    streaming twin for ask().
inline constexpr song::u16 kService_AgentNode_Stream = 2;

}  // namespace savannah_wire

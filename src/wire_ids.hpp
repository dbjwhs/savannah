// MIT License
// Copyright (c) 2026 Dennis B Jones
//
// wire_ids.hpp — mesh service type.
//
// The streaming service-id split (kService_AgentNode_Stream) used to live here
// because songc could not express it. As of song main 316ccf4 (finding 6)
// codegen emits kService_AgentNode_Stream and the streaming dispatchers from
// the IDL `stream` modifier, so the split is gone from here; only the mDNS
// type name remains.

#pragma once

#include <song/types.hpp>

namespace savannah_wire {

// mDNS short type; song expands it to _agent._song._tcp (DESIGN.md section 2).
inline constexpr const char* kMeshServiceType = "agent";

}  // namespace savannah_wire

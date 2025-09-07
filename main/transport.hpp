#pragma once
// Single-inclusion guard for this header (portable alternative to #ifndef/#define).

#include <string>  // std::string for parameters and return value

namespace transport {  // Public HTTP transport interface (ESP-IDF implementation in transport_idf.cpp)

// post_frame
// Purpose:
//   Send a Modbus RTU frame (as an uppercase hex string) to the cloud inverter API
//   and return the "frame" field from the JSON reply.
//
// Behavior:
//   - Chooses endpoint by 'kind': "read" → /api/inverter/read, any other → /api/inverter/write.
//   - Builds a JSON body: {"frame":"<frame_hex>"} and POSTs it with Authorization header.
//   - On a 200 OK with a well-formed JSON body containing "frame", returns that string.
//   - On transport error, non-200 status, blank/malformed JSON, returns an empty string.
//
// Parameters:
//   kind        : Operation selector. Expected values: "read" or "write".
//   base_url    : Base server URL, e.g. "http://<ip>:8080" (no trailing slash required).
//   api_key_b64 : Authorization header value (Base64 of "user:pass"). No "Basic " prefix.
//   frame_hex   : Full Modbus RTU frame as hex (including CRC; CRC is little-endian in RTU).
//
// Returns:
//   std::string with the response frame hex on success; empty string on failure.
//
// Notes:
//   - This function does not validate Modbus semantics; callers should pass the returned
//     string to modbus::parse_read_response or modbus::parse_exception_response.
//   - Empty return can mean: network timeout, HTTP error, non-200, or server-side rejection.
//   - Keep this header minimal; platform-specific details live in transport_idf.cpp.
std::string post_frame(const std::string& kind,
                       const std::string& base_url,
                       const std::string& api_key_b64,
                       const std::string& frame_hex);

} // namespace transport

#pragma once                                         // L1: Prevent multiple inclusion.

#include <string>                                    // L3: std::string for URLs and payloads.

namespace transport {                                // L5: Transport API namespace.

// Post a Modbus RTU frame (ASCII hex) to the Inverter SIM.
// - kind: "read" or "write" → selects /api/inverter/read or /api/inverter/write
// - base_url: e.g., "http://20.15.114.131:8080"
// - api_key_b64: Authorization header value (may be blank if not required)
// - frame_hex: request frame in uppercase ASCII hex (CRC included)
// Returns: response "frame" string from JSON on success, or empty string on error.
std::string post_frame(const std::string& kind,      // L12
                       const std::string& base_url,  // L13
                       const std::string& api_key_b64,// L14
                       const std::string& frame_hex);// L15

} // namespace transport                              // L17
